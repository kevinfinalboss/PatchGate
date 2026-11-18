"""Signed approval links (standard library only, ADR 0010).

A token identifies one approval request and one approver and expires. It carries no decision:
the approver chooses on a confirmation page, so link scanners that open URLs decide nothing.
Single use is enforced by the provider's store, not by the token.

Format: base64url(JSON claims) "." base64url(HMAC-SHA256(secret, claims part)).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MIN_SECRET_BYTES = 32


class TokenError(ValueError):
    """Malformed, tampered or expired token."""


@dataclass(frozen=True)
class Claims:
    request_id: str
    member: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())


def _check_secret(secret: bytes) -> None:
    if len(secret) < MIN_SECRET_BYTES:
        raise TokenError(f"secret must have at least {MIN_SECRET_BYTES} bytes")


def sign(secret: bytes, claims: Claims) -> str:
    _check_secret(secret)
    if not _REQUEST_ID.match(claims.request_id):
        raise TokenError("invalid request id")
    body = {"r": claims.request_id, "m": claims.member.lower(), "e": claims.expires_at}
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{_mac(secret, payload)}"


def verify(secret: bytes, token: str, now: float) -> Claims:
    _check_secret(secret)
    if not isinstance(token, str) or len(token) > 1024 or token.count(".") != 1:
        raise TokenError("malformed token")
    payload, mac = token.split(".")
    if not hmac.compare_digest(mac, _mac(secret, payload)):
        raise TokenError("invalid signature")
    try:
        body = json.loads(_unb64(payload))
        claims = Claims(str(body["r"]), str(body["m"]), int(body["e"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError("malformed token") from exc
    if not _REQUEST_ID.match(claims.request_id):
        raise TokenError("malformed token")
    if now >= claims.expires_at:
        raise TokenError("link expired")
    return claims
