"""The worker's internal JWT (design §3.6; PAY-600, R9, C30).

``iss``, ``sub`` and ``aud`` are constants in code, not settings, so the worker key can't mint
other issuers' tokens. A fresh token per HTTP try; ``cid`` comes from the event.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pycommon import Clock, SystemClock
from pycommon.auth import TokenSigner
from pydantic import SecretStr

__all__ = ["AUDIENCE", "ISSUER", "SUBJECT", "WorkerJwtSigner"]

ISSUER: Final = "payout-worker"
SUBJECT: Final = "service:payout-worker"
AUDIENCE: Final = "payroll"


class WorkerJwtSigner:
    def __init__(
        self, private_key_pem: SecretStr, kid: str, ttl_s: int = 60, clock: Clock | None = None
    ) -> None:
        self._signer = TokenSigner(
            private_key_pem.get_secret_value(),
            kid=kid,
            issuer=ISSUER,
            clock=clock or SystemClock(),
        )
        self._ttl_s = ttl_s

    def mint(self, company_id: UUID) -> str:
        """EdDSA; ``roles=[]``, ``jti``; no ``sid`` and no ``name`` (R9)."""
        return self._signer.mint(
            aud=AUDIENCE, sub=SUBJECT, cid=company_id, roles=(), ttl_s=self._ttl_s
        )
