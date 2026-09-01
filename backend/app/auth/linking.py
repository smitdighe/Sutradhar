"""Resolving a provider identity to a local account.

**Subject first, email second.** The provider's ``sub`` is immutable; the email
attached to it is not. Resolving by email would mean that when a provider
recycles an address -- which Google does for deleted Workspace accounts -- the
next person to hold it inherits the local account. Subject-first resolution
makes that impossible: a new person is a new subject, whatever address they
arrive with.

The corollary is that a provider never rewrites ``users.email``. The local email
is the account's own identity, used for password login and for anything sent to
the user. When a linked Google account changes address, that is recorded in
``oauth_identities.provider_email`` and nowhere else.

**Email is only ever trusted when verified.** An unverified provider email is an
account-takeover primitive: register the victim's address at the provider, sign
in, and get handed their local account. Step 2 below runs only when the provider
asserts verification, and an unverified identity is refused outright rather than
falling through to account creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oauth.base import ProviderIdentity
from app.core.errors import ConflictError, ErrorCode, ValidationError
from app.db.models.enums import AuthEventType, OAuthProvider
from app.db.models.user import AuthEvent, OAuthIdentity, User

__all__ = ["LinkOutcome", "Resolution", "resolve_identity"]


class LinkOutcome(Enum):
    """How an identity resolved."""

    EXISTING_IDENTITY = auto()  # seen this subject before
    LINKED_TO_EXISTING_USER = auto()  # matched a local account by verified email
    NEW_IDENTITY = auto()  # never seen; caller must mint a pending token


@dataclass(frozen=True, slots=True)
class Resolution:
    """Result of resolution. ``user`` is None only for ``NEW_IDENTITY``."""

    outcome: LinkOutcome
    user: User | None = None


def _require_verified_email(identity: ProviderIdentity) -> str:
    if not identity.email_verified or not identity.email:
        raise ValidationError(
            code=ErrorCode.PROVIDER_EMAIL_UNVERIFIED,
            status=400,
            message="the identity provider has not verified this email address",
        )
    return identity.email


async def resolve_identity(session: AsyncSession, identity: ProviderIdentity) -> Resolution:
    """Map a verified provider identity onto a local account.

    Nothing is created here for a new identity -- that needs a role, which only
    the completion step can supply. See :mod:`app.auth.pending`.
    """
    # Refused before anything is looked up, so an unverified identity cannot
    # link, cannot create, and cannot even confirm whether an account exists.
    email = _require_verified_email(identity)

    # --- 1. By subject. The only stable key. ---
    existing = (
        await session.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == OAuthProvider.GOOGLE,
                OAuthIdentity.provider_subject == identity.subject,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # The provider's address may have changed since linking. Record the new
        # one against the identity; users.email is left alone on purpose.
        if existing.provider_email != email:
            existing.provider_email = email
        existing.email_verified = True
        user = await session.get(User, existing.user_id)
        if user is None:  # pragma: no cover - FK guarantees this
            raise ValidationError(
                code=ErrorCode.PROVIDER_EMAIL_UNVERIFIED,
                status=400,
                message="linked account is no longer available",
            )
        await session.flush()
        return Resolution(outcome=LinkOutcome.EXISTING_IDENTITY, user=user)

    # --- 2. By verified email, against local accounts. citext, so case-insensitive. ---
    local = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if local is not None:
        # Does this account already have a Google identity? If so, a *different*
        # Google subject is now presenting the same verified address, which is
        # precisely the recycled-address case: Google released the address, and
        # whoever holds it now is not the person who linked it. Refuse rather
        # than link. (The unique index on (user_id, provider) enforces this
        # regardless; this is the legible path to the same answer.)
        already_linked = (
            await session.execute(
                select(OAuthIdentity).where(
                    OAuthIdentity.user_id == local.id,
                    OAuthIdentity.provider == OAuthProvider.GOOGLE,
                )
            )
        ).scalar_one_or_none()
        if already_linked is not None:
            raise ConflictError(
                code=ErrorCode.OAUTH_IDENTITY_LINKED,
                message="this account is already linked to a different google account",
            )

        # An existing password user signing in with Google lands on the same
        # account. Creating a second user here is the bug this branch exists to
        # prevent -- two accounts for one person, one of which owns their items.
        session.add(
            OAuthIdentity(
                user_id=local.id,
                provider=OAuthProvider.GOOGLE,
                provider_subject=identity.subject,
                provider_email=email,
                email_verified=True,
            )
        )
        session.add(
            AuthEvent(
                user_id=local.id,
                event_type=AuthEventType.OAUTH_LINK,
                detail={"provider": str(OAuthProvider.GOOGLE)},
            )
        )
        await session.flush()
        return Resolution(outcome=LinkOutcome.LINKED_TO_EXISTING_USER, user=local)

    # --- 3. Never seen. The router mints a pending token; no rows written. ---
    return Resolution(outcome=LinkOutcome.NEW_IDENTITY, user=None)
