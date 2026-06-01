"""Shared HMAC signing primitives used by exchange adapters.

Each exchange specifies its own canonical string and digest/encoding; these
helpers cover the combinations the supported exchanges need. Keeping them here
means the per-exchange adapters only assemble the message, not the crypto.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def _digestmod(algorithm: str) -> str:
    if algorithm not in {"sha256", "sha512"}:
        raise ValueError(f"Unsupported HMAC algorithm: {algorithm}")
    return algorithm


def hmac_hex(secret: str, message: str, *, algorithm: str = "sha256") -> str:
    """Hex-encoded HMAC (Bybit, MEXC, Gate use hex digests)."""
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), _digestmod(algorithm)
    ).hexdigest()


def hmac_base64(secret: str, message: str, *, algorithm: str = "sha256") -> str:
    """Base64-encoded HMAC (Bitget, OKX, KuCoin, WEEX use base64 digests)."""
    digest = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), _digestmod(algorithm)
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def sha512_hex(message: str) -> str:
    """Plain SHA-512 hex digest (Gate hashes the request body before signing)."""
    return hashlib.sha512(message.encode("utf-8")).hexdigest()
