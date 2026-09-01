"""Refresh token families, rotation, and reuse detection.

Every login opens a **family**. Each refresh revokes the presented token and
issues a successor in the same family, linked by ``replaced_by``. So a family is
a chain, and at any moment exactly one link in it should be live.

**Reuse detection.** Presenting an already-revoked token means two parties hold
tokens from one chain, which only happens if one was stolen. There is no way to
tell the thief from the victim, so the entire family is revoked: both are logged
out and the victim finds out something is wrong. Leaving the newest token alive
would be choosing to trust whoever refreshed most recently, and that is as
likely to be the attacker.

**Concurrency.** Rotation takes ``SELECT ... FOR UPDATE`` on the token row, so
two simultaneous refreshes of the same token serialise. The first rotates; the
second then re-reads the row, sees ``revoked_at`` set, and treats it as reuse --
killing the family, including the token the first request just issued. That is
the correct outcome: two clients racing on one refresh token is
indistinguishable from theft. Exactly one 200, and the family is dead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import hash_refresh_token, issue_refresh_token
from app.config import get_settings
from app.core.clock import now
from app.core.errors import AuthError, ErrorCode
from app.core.hashing import sha256_hex
from app.db.models.enums import AuthEventType
from app.db.models.user import AuthEvent, RefreshToken

__all__ = [
    "IssuedRefreshToken",
    "hash_client_metadata",
    "issue_family",
    "revoke_all_for_user",
    "revoke_family",
    "rotate",
]


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    """A freshly minted refresh token. ``raw`` leaves the process only once."""

    raw: str
    record: RefreshToken


def hash_client_metadata(value: str | None) -> str | None:
    """SHA-256 an IP or user agent, or pass through None.

    Stored hashed, never raw: correlating sessions across a family is the only
    thing these are for, and that works just as well on a digest.
    """
    return sha256_hex(value.encode("utf-8")) if value else None


def _expiry() -> datetime:
    return now() + timedelta(seconds=get_settings().refresh_token_ttl_seconds)


async def issue_family(
    session: AsyncSession,
    user_id: uuid.UUID,
    ip: str | None = None,
    user_agent: str | None = None,
) -> IssuedRefreshToken:
    """Open a new family. Called on login, never on refresh."""
    raw, token_hash = issue_refresh_token()
    record = RefreshToken(
        user_id=user_id,
        family_id=uuid.uuid4(),
        token_hash=token_hash,
        expires_at=_expiry(),
        ip_hash=hash_client_metadata(ip),
        user_agent_hash=hash_client_metadata(user_agent),
    )
    session.add(record)
    await session.flush()
    return IssuedRefreshToken(raw=raw, record=record)


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> int:
    """Revoke every unrevoked token in one family. Returns the row count."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now())
        ),
    )
    return int(result.rowcount or 0)


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every family belonging to *user_id*. Backs logout-all."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now())
        ),
    )
    return int(result.rowcount or 0)


async def rotate(
    session: AsyncSession,
    presented: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> IssuedRefreshToken:
    """Exchange a refresh token for its successor.

    Raises :class:`AuthError` for an unknown, reused, or expired token. The
    reuse case revokes the whole family before raising.
    """
    token_hash = hash_refresh_token(presented)

    # FOR UPDATE: serialises concurrent rotations of the same token, so the
    # loser observes the winner's revocation instead of both succeeding.
    current = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
    ).scalar_one_or_none()

    if current is None:
        raise AuthError(
            code=ErrorCode.INVALID_REFRESH_TOKEN, message="refresh token is not valid"
        )

    if current.revoked_at is not None:
        await _record_reuse(session, current)
        await session.commit()
        raise AuthError(
            code=ErrorCode.REFRESH_TOKEN_REUSED,
            message="refresh token was already used; all sessions in this family are revoked",
        )

    if current.expires_at <= now():
        raise AuthError(
            code=ErrorCode.REFRESH_TOKEN_EXPIRED, message="refresh token has expired"
        )

    raw, new_hash = issue_refresh_token()
    successor = RefreshToken(
        user_id=current.user_id,
        family_id=current.family_id,
        token_hash=new_hash,
        expires_at=_expiry(),
        ip_hash=hash_client_metadata(ip),
        user_agent_hash=hash_client_metadata(user_agent),
    )
    session.add(successor)
    await session.flush()

    # Same transaction as the insert: a crash between the two would either
    # leave two live tokens or none.
    current.revoked_at = now()
    current.replaced_by = successor.id
    await session.flush()

    return IssuedRefreshToken(raw=raw, record=successor)


async def _record_reuse(session: AsyncSession, token: RefreshToken) -> None:
    """Kill the family and write the audit event. Never records the token itself."""
    await revoke_family(session, token.family_id)
    session.add(
        AuthEvent(
            user_id=token.user_id,
            event_type=AuthEventType.REFRESH_REUSE_DETECTED,
            ip_hash=token.ip_hash,
            user_agent_hash=token.user_agent_hash,
            # family_id is safe to record; the token hash is not, because it is
            # the lookup key and logs are a lower-trust store than the database.
            detail={"family_id": str(token.family_id)},
        )
    )
    await session.flush()
