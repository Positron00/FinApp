"""Client IP normalisation and keyed hashes (identity LLD §6.4, §6.8; PAY-NFR-08)."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress

__all__ = ["email_key", "ip_hash", "normalise_ip"]


def normalise_ip(raw: str) -> str:
    """The compressed form; IPv4-mapped IPv6 becomes IPv4. Raises ``ValueError`` when invalid."""
    addr = ipaddress.ip_address(raw.strip())
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.compressed
    return addr.compressed


def ip_hash(ip: str, key: bytes) -> str:
    """HMAC-SHA256(key, normalised ip), 64 hex characters."""
    return hmac.new(key, normalise_ip(ip).encode(), hashlib.sha256).hexdigest()


def email_key(email: str, key: bytes) -> str:
    """HMAC-SHA256(key, lower-cased email) hex: the Redis subject for an account."""
    return hmac.new(key, email.strip().lower().encode(), hashlib.sha256).hexdigest()
