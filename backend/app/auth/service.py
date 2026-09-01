"""Registration, login, and profile updates.

Transport concerns (cookies, headers, rate limiting) live in the router. This
module owns the rules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from app.auth.roles import Role, is_self_assignable
from app.auth.schemas import LoginRequest, RegisterRequest, UpdateProfileRequest
from app.auth.sessions import hash_client_metadata
from app.core.clock import now
from app.core.crypto_shred import new_salt
from app.core.errors import AuthError, ConflictError, ErrorCode, ForbiddenError
from app.db.models.enums import AuthEventType, UserStatus
from app.db.models.user import AuthEvent, User

__all__ = ["ClientContext", "authenticate", "record_auth_event", "register", "update_profile"]


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Whatever the transport knows about the caller. Hashed before storage."""

    ip: str | None = None
    user_agent: str | None = None


async def record_auth_event(
    session: AsyncSession,
    event_type: AuthEventType,
    user_id: uuid.UUID | None,
    client: ClientContext,
    detail: dict[str, object] | None = None,
) -> None:
    """Append one audit row. Never carries a password or a token."""
    session.add(
        AuthEvent(
            user_id=user_id,
            event_type=event_type,
            ip_hash=hash_client_metadata(client.ip),
            user_agent_hash=hash_client_metadata(client.user_agent),
            detail=detail,
        )
    )
    await session.flush()


async def register(
    session: AsyncSession, payload: RegisterRequest, client: ClientContext
) -> User:
    """Create an account, or raise on a duplicate email or a forbidden role."""
    requested_role = payload.role or Role.CONSUMER
    if not is_self_assignable(requested_role):
        raise ForbiddenError(
            code=ErrorCode.ROLE_NOT_SELF_ASSIGNABLE,
            message=f"role {requested_role} must be granted by an administrator",
            details={"role": str(requested_role)},
        )

    validate_password_policy(payload.password, payload.email)

    # A self-declared weaver is not a trusted weaver. Landing in
    # PENDING_VERIFICATION means the claim exists but confers nothing until a
    # human checks it -- the Phase 8 trust model reads this distinction.
    status = (
        UserStatus.PENDING_VERIFICATION if requested_role is Role.WEAVER else UserStatus.ACTIVE
    )

    user = User(
        email=str(payload.email),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        role=requested_role,
        status=status,
        region=payload.region,
        org_name=payload.org_name,
        identity_salt=new_salt(),
    )
    session.add(user)

    # 409 rather than a generic 201. That leaks which emails are registered,
    # which for a public sign-up form is a real (if minor) enumeration vector.
    # The tradeoff is taken knowingly: a silent 201 means a user who actually
    # forgot they had an account gets a success page and no account, and for
    # this project's threat model that support burden outweighs the leak.
    # Revisit if registration ever becomes invite-only or privacy-sensitive.
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            code=ErrorCode.EMAIL_ALREADY_REGISTERED,
            message="an account with this email already exists",
        ) from exc

    await record_auth_event(
        session,
        AuthEventType.REGISTER,
        user.id,
        client,
        {"role": str(requested_role), "status": str(status)},
    )
    return user


async def authenticate(
    session: AsyncSession, payload: LoginRequest, client: ClientContext
) -> User:
    """Verify credentials and return the user, or raise.

    Both failure modes -- no such user, wrong password -- raise the identical
    ``INVALID_CREDENTIALS`` and perform the same argon2 work, so neither the
    response nor the timing distinguishes them.
    """
    user = (
        await session.execute(select(User).where(User.email == str(payload.email)))
    ).scalar_one_or_none()

    stored_hash = user.password_hash if user and user.password_hash else DUMMY_HASH
    password_ok = verify_password(payload.password, stored_hash)

    # `user is None` and a wrong password converge here having done equal work.
    # An OAuth-only account (no password_hash) also lands here: it verified
    # against the dummy, so it can never match.
    if user is None or user.password_hash is None or not password_ok:
        await record_auth_event(
            session,
            AuthEventType.LOGIN_FAILURE,
            user.id if user else None,
            client,
            {"reason": "invalid_credentials"},
        )
        await session.commit()
        raise AuthError(
            code=ErrorCode.INVALID_CREDENTIALS, message="email or password is incorrect"
        )

    if user.status is UserStatus.SUSPENDED:
        await record_auth_event(
            session,
            AuthEventType.LOGIN_FAILURE,
            user.id,
            client,
            {"reason": "account_suspended"},
        )
        await session.commit()
        raise ForbiddenError(
            code=ErrorCode.ACCOUNT_SUSPENDED, message="this account is suspended"
        )

    # Transparent upgrade: the password is in hand and verified exactly once
    # per login, so this is the only moment a stronger hash can be written
    # without asking the user for anything.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.last_login_at = now()
    await record_auth_event(session, AuthEventType.LOGIN_SUCCESS, user.id, client)
    return user


async def update_profile(
    session: AsyncSession, user: User, payload: UpdateProfileRequest
) -> User:
    """Apply the two mutable profile fields. Nothing else is reachable here."""
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.region is not None:
        user.region = payload.region
    await session.flush()
    return user
