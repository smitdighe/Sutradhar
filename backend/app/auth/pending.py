"""The pending token: a signed ticket for a half-finished sign-up.

**Why this hop exists at all.** A Google profile carries a name and an email. It
does not carry a *role*, and this system's entire trust model turns on the
CONSUMER/WEAVER distinction -- a weaver registers items whose provenance other
people rely on. Role cannot be inferred, cannot be defaulted safely, and
certainly cannot be taken from the provider. So a callback for an identity that
has never been seen before has no way to create a usable account, and issuing a
session first and asking for a role afterwards would mean a live session for an
account whose role is undecided.

Hence: the callback mints a pending token and sends the browser to the frontend
to collect the missing fields. ``/auth/oauth/complete`` presents the token back
along with a role, and only then does an account and a session exist.

**What a leaked pending token gets an attacker.** It is deliberately close to
nothing. The claims name a provider identity that has already been verified, so
the worst outcome is that somebody creates an account for an email address they
already demonstrably control at Google. It cannot authenticate anywhere:

* Different key. HS256 with ``PENDING_TOKEN_SECRET``, not the Ed25519 session
  keypair. Presenting it as a bearer token fails signature verification outright.
* Different audience. ``sutradhar/pending``, so even a token signed with the
  right key would fail the session audience check in
  :func:`app.auth.tokens.decode_access_token`.
* No authority in the claims. No role, no user id, no scopes -- nothing a
  client could influence and nothing a downstream check could mistake for
  permission.
* Single use, enforced by a conditional UPDATE, and ten minutes to live.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oauth.base import ProviderIdentity
from app.config import get_settings
from app.core.clock import now
from app.core.errors import AuthError, ErrorCode
from app.db.models.enums import OAuthProvider
from app.db.models.user import PendingToken

__all__ = ["PendingClaims", "burn_pending_token", "issue_pending_token", "read_pending_token"]

# HS256, not the session EdDSA key. Deliberate: the two token families must not
# be interchangeable even by accident.
_ALGORITHM = "HS256"


@dataclass(frozen=True, slots=True)
class PendingClaims:
    """The verified contents of a pending token. Carries no authority."""

    jti: uuid.UUID
    provider: OAuthProvider
    provider_subject: str
    provider_email: str | None


async def issue_pending_token(session: AsyncSession, identity: ProviderIdentity) -> str:
    """Record a pending sign-up and return its signed token."""
    settings = get_settings()
    jti = uuid.uuid4()
    issued_at = now()
    expires_at = issued_at + timedelta(seconds=settings.pending_token_ttl_seconds)

    # The row is what makes single use enforceable; the JWT is only transport.
    session.add(
        PendingToken(
            jti=jti,
            provider=OAuthProvider.GOOGLE,
            provider_subject=identity.subject,
            provider_email=identity.email,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    )
    await session.flush()

    payload = {
        "jti": str(jti),
        "provider": str(OAuthProvider.GOOGLE),
        "provider_subject": identity.subject,
        "provider_email": identity.email,
        "iss": settings.jwt_issuer,
        "aud": settings.pending_token_audience,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.pending_token_secret, algorithm=_ALGORITHM)


def read_pending_token(token: str) -> PendingClaims:
    """Verify a pending token's signature, audience, and expiry."""
    settings = get_settings()
    invalid = AuthError(
        code=ErrorCode.TOKEN_INVALID, message="pending token is not valid"
    )
    try:
        payload = jwt.decode(
            token,
            settings.pending_token_secret,
            algorithms=[_ALGORITHM],
            audience=settings.pending_token_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": ["jti", "aud", "iss", "exp", "iat", "provider_subject"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except ExpiredSignatureError as exc:
        raise AuthError(
            code=ErrorCode.TOKEN_EXPIRED, message="pending token has expired"
        ) from exc
    except InvalidTokenError as exc:
        raise invalid from exc

    try:
        return PendingClaims(
            jti=uuid.UUID(str(payload["jti"])),
            provider=OAuthProvider(str(payload["provider"])),
            provider_subject=str(payload["provider_subject"]),
            provider_email=payload.get("provider_email"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise invalid from exc


async def burn_pending_token(session: AsyncSession, jti: uuid.UUID) -> PendingToken:
    """Consume the token exactly once, or raise ``PENDING_TOKEN_CONSUMED``.

    A single conditional UPDATE, never a read-then-write: the ``consumed_at IS
    NULL`` predicate and the write happen in one statement, so two concurrent
    completions cannot both observe an unconsumed row.
    """
    claimed = (
        await session.execute(
            update(PendingToken)
            .where(PendingToken.jti == jti, PendingToken.consumed_at.is_(None))
            .values(consumed_at=now())
            .returning(PendingToken)
        )
    ).scalar_one_or_none()

    if claimed is None:
        # Covers both "already used" and "never existed"; the caller learns
        # nothing about which, and neither is recoverable.
        raise AuthError(
            code=ErrorCode.PENDING_TOKEN_CONSUMED,
            message="this sign-up link has already been used or has expired",
        )
    if claimed.expires_at <= now():
        raise AuthError(code=ErrorCode.TOKEN_EXPIRED, message="pending token has expired")
    return claimed
