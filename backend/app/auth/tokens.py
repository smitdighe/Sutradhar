"""Access tokens (signed JWTs) and refresh tokens (opaque secrets).

The two are deliberately different kinds of thing.

**Access token** -- a short-lived Ed25519-signed JWT. Stateless, so verifying it
costs no database round trip, which is what makes a 15-minute TTL affordable on
every request. Ed25519 over RSA because the signatures and keys are small and
signing is fast; over HMAC because the public key can be handed to a verifier
that must not be able to mint tokens.

**Refresh token** -- 32 random bytes, not a JWT. It is long-lived, so it must be
revocable, and revocation means server-side state. Since it has to be looked up
anyway there is nothing for a signature to buy. Only the SHA-256 of it is
stored: a stolen database yields no usable token.

Verification pins the algorithm, the issuer and the audience explicitly. Pinning
the algorithm is what stops the ``alg: none`` and HS256-signed-with-the-public-key
substitutions; pinning the audience is what stops a Phase 3 pending token from
being spent as a session token.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from app.config import get_settings
from app.core.clock import now
from app.core.errors import AuthError, ErrorCode
from app.core.hashing import sha256_hex

__all__ = [
    "ACCESS_TOKEN_VERSION",
    "REFRESH_TOKEN_BYTES",
    "AccessTokenClaims",
    "decode_access_token",
    "hash_refresh_token",
    "issue_access_token",
    "issue_refresh_token",
]

# Bumped when the claim set changes shape. Lets a verifier reject tokens minted
# by an older deployment instead of guessing at missing claims.
ACCESS_TOKEN_VERSION = 1

REFRESH_TOKEN_BYTES = 32

# Only ever this one. Passed to jwt.decode as the sole permitted algorithm.
_ALGORITHM = "EdDSA"


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """The verified contents of an access token."""

    subject: uuid.UUID
    role: str
    jti: str
    issued_at: datetime
    expires_at: datetime
    version: int


@lru_cache(maxsize=1)
def _private_key() -> bytes:
    return get_settings().jwt_private_key_path.read_bytes()


@lru_cache(maxsize=1)
def _public_key() -> bytes:
    return get_settings().jwt_public_key_path.read_bytes()


def issue_access_token(user_id: uuid.UUID, role: str) -> tuple[str, int]:
    """Mint an access token. Returns ``(token, expires_in_seconds)``."""
    settings = get_settings()
    issued_at = now()
    ttl = settings.access_token_ttl_seconds
    expires_at = int(issued_at.timestamp()) + ttl

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": str(role),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(issued_at.timestamp()),
        "exp": expires_at,
        "jti": uuid.uuid4().hex,
        "ver": ACCESS_TOKEN_VERSION,
    }
    token = jwt.encode(payload, _private_key(), algorithm=_ALGORITHM)
    return token, ttl


def decode_access_token(token: str) -> AccessTokenClaims:
    """Verify and decode an access token, or raise :class:`AuthError`.

    ``algorithms`` is a single-element list on purpose. Accepting a list that
    includes ``none``, or accepting an HS algorithm, would let a caller sign a
    token with the *public* key -- which is public -- and be believed.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            _public_key(),
            algorithms=[_ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={
                "require": ["sub", "iss", "aud", "exp", "iat", "jti"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except ExpiredSignatureError as exc:
        raise AuthError(code=ErrorCode.TOKEN_EXPIRED, message="access token expired") from exc
    except InvalidTokenError as exc:
        # Deliberately one generic code for every structural failure: a bad
        # signature, a wrong issuer, and a pending-audience token all look the
        # same to the caller. In particular a Phase 3 pending token fails the
        # audience check here and is rejected as 401, never 403 -- it is not an
        # under-privileged session, it is not a session at all.
        raise AuthError(code=ErrorCode.TOKEN_INVALID, message="access token is not valid") from exc

    try:
        return AccessTokenClaims(
            subject=uuid.UUID(str(payload["sub"])),
            role=str(payload.get("role", "")),
            jti=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=now().tzinfo),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=now().tzinfo),
            version=int(payload.get("ver", 0)),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AuthError(code=ErrorCode.TOKEN_INVALID, message="access token is not valid") from exc


def issue_refresh_token() -> tuple[str, str]:
    """Mint an opaque refresh token. Returns ``(raw, sha256_hex)``.

    The raw value is returned to the caller exactly once, in the response that
    creates it. Only the digest is ever persisted.
    """
    raw = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    """SHA-256 hex of a raw refresh token, as stored in ``refresh_tokens``."""
    return sha256_hex(raw.encode("utf-8"))
