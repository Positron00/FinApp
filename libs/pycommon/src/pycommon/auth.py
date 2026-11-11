"""Internal JWT: issuer-bound keyset, verify, sign, FastAPI dependencies (platform LLD §3.5).

PAY-600 (EdDSA tokens, issuer bound to the kid), PAY-601 and the 401/403 rule of C30. The token,
``name`` and ``sid`` are never logged.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, get_args
from uuid import UUID

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import Depends, Request

from pycommon import Clock, SystemClock
from pycommon.errors import Forbidden, Unauthorized
from pycommon.logging import get_logger, log_event

__all__ = [
    "Issuer",
    "KeyEntry",
    "Keyset",
    "Principal",
    "SubjectKind",
    "TokenRejected",
    "TokenSigner",
    "TokenVerifier",
    "authenticate",
    "require",
]

Issuer = Literal["web", "payout-worker"]
SubjectKind = Literal["user", "service:web", "service:payout-worker"]
RejectReason = Literal[
    "missing",
    "malformed",
    "unknown_kid",
    "bad_alg",
    "bad_signature",
    "expired",
    "not_yet_valid",
    "wrong_aud",
    "issuer_mismatch",
    "issuer_not_allowed",
    "missing_claim",
    "lifetime_too_long",
]

ISSUERS: Final[frozenset[str]] = frozenset(get_args(Issuer))
REQUIRED_CLAIMS: Final = ["exp", "iat", "iss", "aud", "sub", "jti"]
SERVICE_SUBJECTS: Final[frozenset[str]] = frozenset({"service:web", "service:payout-worker"})

_log = get_logger("pycommon.auth")


def _settings_error(message: str) -> Exception:
    from pycommon.settings import SettingsError  # noqa: PLC0415  (settings imports this module)

    return SettingsError(message=message)


def _normalise_pem(value: str) -> str:
    from pycommon.settings import normalise_pem  # noqa: PLC0415

    return normalise_pem(value)


@dataclass(frozen=True)
class KeyEntry:
    kid: str
    iss: Issuer
    public_key: Ed25519PublicKey


class Keyset:
    """``INTERNAL_JWT_PUBLIC_KEYS``: ``{kid: {"iss": "web"|"payout-worker", "pem": "..."}}``."""

    def __init__(self, entries: Iterable[KeyEntry]) -> None:
        self._entries: dict[str, KeyEntry] = {}
        for e in entries:
            if e.kid in self._entries:
                raise _settings_error(f"keyset: duplicate kid {e.kid!r}")
            self._entries[e.kid] = e

    @classmethod
    def from_json(cls, raw: str) -> Keyset:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            seen: dict[str, Any] = {}
            for k, v in items:
                if k in seen:
                    raise _settings_error(f"keyset: duplicate kid {k!r}")
                seen[k] = v
            return seen

        try:
            data = json.loads(raw, object_pairs_hook=pairs)
        except ValueError:
            raise _settings_error("keyset: not valid JSON") from None
        if not isinstance(data, dict) or not data:
            raise _settings_error("keyset: must be a non-empty JSON object")
        entries: list[KeyEntry] = []
        for kid, spec in data.items():
            if not isinstance(spec, dict):
                raise _settings_error(f"keyset: entry {kid!r} must be an object")
            iss = spec.get("iss")
            pem = spec.get("pem")
            if iss not in ISSUERS:
                raise _settings_error(f"keyset: entry {kid!r} has an unknown iss")
            if not isinstance(pem, str):
                raise _settings_error(f"keyset: entry {kid!r} has no pem")
            try:
                key = serialization.load_pem_public_key(_normalise_pem(pem).encode())
            except ValueError:
                raise _settings_error(f"keyset: entry {kid!r} is not a PEM public key") from None
            if not isinstance(key, Ed25519PublicKey):
                raise _settings_error(f"keyset: entry {kid!r} is not an Ed25519 key")
            entries.append(KeyEntry(kid=kid, iss=cast(Issuer, iss), public_key=key))
        return cls(entries)

    def get(self, kid: str) -> KeyEntry | None:
        return self._entries.get(kid)

    @property
    def kids(self) -> frozenset[str]:
        return frozenset(self._entries)

    def __repr__(self) -> str:
        return f"Keyset(kids={sorted(self._entries)!r})"


@dataclass(frozen=True)
class Principal:
    iss: Issuer
    sub: str
    roles: frozenset[str]
    cid: UUID
    name: str | None
    sid: str | None
    jti: str
    exp: datetime

    @property
    def is_service(self) -> bool:
        return self.sub.startswith("service:")

    @property
    def user_id(self) -> UUID | None:
        if self.is_service:
            return None
        try:
            return UUID(self.sub)
        except ValueError:
            return None

    @property
    def caller_key(self) -> str:
        return f"{self.iss}:{self.sub}"

    @property
    def subject_kind(self) -> SubjectKind | None:
        if self.sub in SERVICE_SUBJECTS:
            return cast(SubjectKind, self.sub)
        return "user" if self.user_id is not None else None

    def __repr__(self) -> str:  # never shows name or sid
        return f"Principal(iss={self.iss!r}, sub={self.sub!r}, roles={sorted(self.roles)!r})"


class TokenRejected(Unauthorized):
    """401 ``invalid_token``; ``reason`` is logged, never returned."""

    def __init__(self, reason: RejectReason) -> None:
        self.reason: RejectReason = reason
        super().__init__("Invalid or missing token.")


def _short(value: object) -> str | None:
    return value[:64] if isinstance(value, str) else None


def _reject(reason: RejectReason, *, kid: object = None, iss: object = None) -> TokenRejected:
    log_event(
        _log,
        logging.WARNING,
        "auth.token.rejected",
        reason=reason,
        kid=_short(kid),
        iss=iss if iss in ISSUERS else None,
    )
    return TokenRejected(reason)


class TokenVerifier:
    def __init__(
        self,
        keyset: Keyset,
        *,
        audience: str,
        leeway_s: int = 5,
        max_lifetime_s: int = 60,
        clock: Clock | None = None,
    ) -> None:
        self._keyset = keyset
        self._audience = audience
        self._leeway = leeway_s
        self._max_lifetime = max_lifetime_s
        self._clock = clock or SystemClock()

    def verify(self, token: str) -> Principal:
        if not token:
            raise _reject("missing")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise _reject("malformed") from None
        kid = header.get("kid")
        if header.get("alg") != "EdDSA":
            raise _reject("bad_alg", kid=kid)
        entry = self._keyset.get(kid) if isinstance(kid, str) else None
        if entry is None:
            raise _reject("unknown_kid", kid=kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                entry.public_key,
                algorithms=["EdDSA"],
                audience=self._audience,
                options={
                    "require": REQUIRED_CLAIMS,
                    # Time claims are checked below against the injectable clock.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidAudienceError:
            raise _reject("wrong_aud", kid=kid, iss=entry.iss) from None
        except jwt.InvalidSignatureError:
            raise _reject("bad_signature", kid=kid, iss=entry.iss) from None
        except jwt.MissingRequiredClaimError:
            raise _reject("missing_claim", kid=kid, iss=entry.iss) from None
        except jwt.PyJWTError:
            raise _reject("malformed", kid=kid, iss=entry.iss) from None
        return self._check_claims(claims, entry)

    def _check_claims(self, claims: dict[str, Any], entry: KeyEntry) -> Principal:
        kid, iss = entry.kid, claims.get("iss")
        exp, iat, nbf = claims.get("exp"), claims.get("iat"), claims.get("nbf")
        numbers = (exp, iat) if nbf is None else (exp, iat, nbf)
        if not all(isinstance(n, int | float) and not isinstance(n, bool) for n in numbers):
            raise _reject("malformed", kid=kid, iss=entry.iss)
        exp_n, iat_n = cast(float, exp), cast(float, iat)
        now = self._clock.now().timestamp()
        if now > exp_n + self._leeway:
            raise _reject("expired", kid=kid, iss=entry.iss)
        if iat_n > now + self._leeway or (
            isinstance(nbf, int | float) and nbf > now + self._leeway
        ):
            raise _reject("not_yet_valid", kid=kid, iss=entry.iss)
        if iss != entry.iss:
            raise _reject("issuer_mismatch", kid=kid, iss=entry.iss)
        if exp_n - iat_n > self._max_lifetime:
            raise _reject("lifetime_too_long", kid=kid, iss=entry.iss)
        try:
            cid = UUID(str(claims["cid"]))
        except KeyError, ValueError:
            raise _reject("missing_claim", kid=kid, iss=entry.iss) from None
        sub, jti = claims.get("sub"), claims.get("jti")
        name, sid = claims.get("name"), claims.get("sid")
        roles = claims.get("roles", [])
        if (
            not isinstance(sub, str)
            or not isinstance(jti, str)
            or (name is not None and not (isinstance(name, str) and 1 <= len(name) <= 200))
            or (sid is not None and not isinstance(sid, str))
            or not isinstance(roles, list)
            or not all(isinstance(r, str) for r in roles)
        ):
            raise _reject("malformed", kid=kid, iss=entry.iss)
        return Principal(
            iss=entry.iss,
            sub=sub,
            roles=frozenset(roles),
            cid=cid,
            name=name,
            sid=sid,
            jti=jti,
            exp=datetime.fromtimestamp(exp_n, UTC),
        )


class TokenSigner:
    def __init__(
        self, private_key_pem: str, *, kid: str, issuer: Issuer, clock: Clock | None = None
    ) -> None:
        key = serialization.load_pem_private_key(_normalise_pem(private_key_pem).encode(), None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("TokenSigner needs an Ed25519 private key")
        if issuer not in ISSUERS:
            raise ValueError("unknown issuer")
        self._key = key
        self._kid = kid
        self._issuer: Issuer = issuer
        self._clock = clock or SystemClock()

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def issuer(self) -> Issuer:
        return self._issuer

    def mint(
        self,
        *,
        aud: str,
        sub: str,
        cid: UUID,
        roles: Iterable[str] = (),
        name: str | None = None,
        sid: str | None = None,
        ttl_s: int = 60,
    ) -> str:
        iat = int(self._clock.now().timestamp())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": aud,
            "sub": sub,
            "cid": str(cid),
            "roles": list(roles),
            "iat": iat,
            "exp": iat + ttl_s,
            "jti": str(uuid.uuid4()),
        }
        if name is not None:
            claims["name"] = name
        if sid is not None:
            claims["sid"] = sid
        return jwt.encode(claims, self._key, algorithm="EdDSA", headers={"kid": self._kid})


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", None) or "unmatched")


async def authenticate(request: Request) -> Principal:
    """FastAPI dependency: verify ``Authorization: Bearer``; sets ``request.state.principal``."""
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached
    ctx = getattr(request.app.state, "ctx", None)
    verifier = getattr(ctx, "verifier", None)
    if not isinstance(verifier, TokenVerifier):
        raise RuntimeError("authenticate needs AppContext.verifier (create_app use_jwt=True)")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if not header:
        raise _reject("missing")
    if scheme.lower() != "bearer" or not token.strip():
        raise _reject("malformed")
    principal = verifier.verify(token.strip())
    request.state.principal = principal
    return principal


def require(
    *,
    issuers: Iterable[Issuer] = ("web",),
    subjects: Iterable[SubjectKind] = ("user",),
    roles: Iterable[str] | None = None,
    any_role: bool = True,
) -> Callable[..., Awaitable[Principal]]:
    """Per-route authorization by the C30 rule: wrong issuer → 401, wrong kind or role → 403."""
    allowed_iss = frozenset(issuers)
    allowed_subjects = frozenset(subjects)
    wanted = frozenset(roles) if roles is not None else None

    async def _require(request: Request, principal: Principal = Depends(authenticate)) -> Principal:  # noqa: B008
        if principal.iss not in allowed_iss:
            raise _reject("issuer_not_allowed", iss=principal.iss)
        kind = principal.subject_kind
        if kind not in allowed_subjects:
            log_event(
                _log, logging.INFO, "auth.forbidden", route=_route_path(request), subject_kind=kind
            )
            raise Forbidden()
        if wanted:
            missing = (
                (wanted if not (wanted & principal.roles) else frozenset())
                if any_role
                else wanted - principal.roles
            )
            if missing:
                log_event(
                    _log,
                    logging.INFO,
                    "auth.forbidden",
                    route=_route_path(request),
                    missing_roles=sorted(missing),
                )
                raise Forbidden()
        return principal

    return _require
