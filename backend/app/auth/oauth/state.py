"""Signed, single-use ``state`` for the authorization request.

State carries three jobs at once:

* **CSRF defence.** A callback arriving with a state this server did not mint is
  somebody else's authorization code being grafted onto this browser.
* **PKCE verifier transport.** The verifier must survive the round trip to
  Google without being stored server-side per in-flight request.
* **Return destination.** Where to send the browser afterwards.

Signing alone is not enough. A signature proves this server minted the value; it
says nothing about whether it has already been spent. So each state carries a
nonce, and the nonce is claimed exactly once by an atomic insert into
``oauth_states`` -- see :mod:`app.db.models.oauth_state`.

``return_to`` is checked against the configured origins. An unvalidated redirect
target here is a phishing vector: an attacker sends a victim through a real
Google consent screen on this server's real domain, and lands them on a page the
attacker controls.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.clock import now
from app.core.errors import ErrorCode, ValidationError
from app.db.models.oauth_state import OAuthState

__all__ = [
    "STATE_MAX_AGE_SECONDS",
    "OAuthState_",
    "StatePayload",
    "claim_nonce",
    "is_allowed_return_to",
    "issue_state",
    "read_state",
]

STATE_MAX_AGE_SECONDS = 600
_SALT = "sutradhar.oauth.state"

# Alias so callers can import the model from here without a second import line.
OAuthState_ = OAuthState


@dataclass(frozen=True, slots=True)
class StatePayload:
    """What a validated state carried."""

    nonce: str
    pkce_verifier: str
    return_to: str | None = None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().oauth_state_secret, salt=_SALT)


def _allowed_origins() -> set[str]:
    """Origins a ``return_to`` may point at.

    Derived from CORS_ALLOWED_ORIGINS plus the app's own base URL -- the same
    places already trusted to talk to this API.
    """
    settings = get_settings()
    origins = set(settings.cors_origins)
    origins.add(settings.app_base_url)
    normalised = set()
    for origin in origins:
        parts = urlsplit(origin)
        if parts.scheme and parts.netloc:
            normalised.add(f"{parts.scheme}://{parts.netloc}")
    return normalised


def is_allowed_return_to(target: str | None) -> bool:
    """True when *target* is absent or points at an allowed origin.

    Rejects protocol-relative (``//evil.test``) and scheme-less values as well,
    since both are read as off-origin by a browser.
    """
    if not target:
        return True
    parts = urlsplit(target)
    if not parts.scheme or not parts.netloc:
        return False
    return f"{parts.scheme}://{parts.netloc}" in _allowed_origins()


def issue_state(pkce_verifier: str, return_to: str | None = None) -> tuple[str, str]:
    """Mint a signed state. Returns ``(state, nonce)``.

    The nonce is returned so the caller can see what will be claimed at
    callback time; nothing is written to the database until then.
    """
    if not is_allowed_return_to(return_to):
        raise ValidationError(
            code=ErrorCode.VALIDATION_FAILED, message="return_to is not an allowed origin"
        )
    nonce = secrets.token_urlsafe(24)
    payload: dict[str, Any] = {"nonce": nonce, "v": pkce_verifier}
    if return_to:
        payload["r"] = return_to
    return _serializer().dumps(payload), nonce


def read_state(state: str) -> StatePayload:
    """Verify a state's signature and age. Does not claim the nonce."""
    invalid = ValidationError(
        code=ErrorCode.OAUTH_STATE_INVALID,
        status=400,
        message="oauth state is missing, expired, or invalid",
    )
    try:
        payload = _serializer().loads(state, max_age=STATE_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature) as exc:
        raise invalid from exc

    if not isinstance(payload, dict):
        raise invalid
    nonce, verifier = payload.get("nonce"), payload.get("v")
    if not isinstance(nonce, str) or not isinstance(verifier, str):
        raise invalid

    return_to = payload.get("r")
    if return_to is not None and not isinstance(return_to, str):
        raise invalid
    # Re-checked on the way out as well as the way in: the allowlist may have
    # been tightened while this state was in flight.
    if not is_allowed_return_to(return_to):
        raise invalid

    return StatePayload(nonce=nonce, pkce_verifier=verifier, return_to=return_to)


async def claim_nonce(session: AsyncSession, nonce: str) -> None:
    """Spend a nonce exactly once, or raise ``OAUTH_STATE_INVALID``.

    One statement: the insert *is* the claim, so two concurrent callbacks
    carrying the same state cannot both proceed. A read-then-write here would
    leave a window between the check and the mark.
    """
    statement = (
        insert(OAuthState)
        .values(nonce=nonce, expires_at=now() + timedelta(seconds=STATE_MAX_AGE_SECONDS))
        .on_conflict_do_nothing(index_elements=[OAuthState.nonce])
        .returning(OAuthState.nonce)
    )
    claimed = (await session.execute(statement)).scalar_one_or_none()
    await session.commit()
    if claimed is None:
        raise ValidationError(
            code=ErrorCode.OAUTH_STATE_INVALID,
            status=400,
            message="oauth state is missing, expired, or invalid",
        )
