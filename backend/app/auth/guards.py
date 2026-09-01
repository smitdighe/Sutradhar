"""Dependencies that turn a bearer token into a :class:`User`, or a 401.

Everything that can go wrong with a credential is a 401, never a 403. The
distinction matters: 403 says "you are somebody, but not somebody allowed to do
this", and answering that to an unverified token concedes that the token
identified somebody. Only :func:`require_role` and
:func:`require_self_or_admin` -- which run *after* authentication -- return 403.

A Phase 3 pending token is rejected here as 401. It is signed by a different
key with a different audience, so it fails verification in
:func:`~app.auth.tokens.decode_access_token` before anything looks at who it
refers to. It is not an under-privileged session; it is not a session.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.roles import Role
from app.auth.tokens import decode_access_token
from app.core.errors import AuthError, ErrorCode, ForbiddenError
from app.db.models.enums import UserStatus
from app.db.models.user import User
from app.db.session import get_session

__all__ = [
    "get_current_user",
    "get_optional_user",
    "require_role",
    "require_self_or_admin",
]

# auto_error=False so a missing header raises this module's AuthError with the
# project's error envelope, rather than Starlette's bare 403 detail body.
_bearer = HTTPBearer(auto_error=False)


async def _resolve_user(token: str, session: AsyncSession, request: Request) -> User:
    claims = decode_access_token(token)
    user = await session.get(User, claims.subject)
    if user is None:
        # Signature was good but the account is gone. Same 401 as a bad token:
        # the caller does not need to learn that this id once existed.
        raise AuthError(code=ErrorCode.TOKEN_INVALID, message="access token is not valid")
    if user.status is UserStatus.SUSPENDED:
        raise AuthError(code=ErrorCode.ACCOUNT_SUSPENDED, message="this account is suspended")
    # Published for the access log, which runs in an outer middleware and has no
    # business parsing a token of its own. `request.state` is backed by the ASGI
    # scope, which is the same object upstream and downstream, so a value set
    # here is readable after the response comes back -- a contextvar would not
    # be, because the downstream app runs in its own task.
    #
    # The id and nothing else. An email in a log line is the leak this system
    # takes the most trouble elsewhere to avoid.
    request.state.user_id = user.id
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Require a valid bearer token and return its user."""
    if credentials is None or not credentials.credentials:
        raise AuthError(code=ErrorCode.UNAUTHENTICATED, message="authentication required")
    if credentials.scheme.lower() != "bearer":
        raise AuthError(code=ErrorCode.UNAUTHENTICATED, message="authentication required")
    return await _resolve_user(credentials.credentials, session, request)


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """Return the user when a valid token is present, else ``None``.

    An *invalid* token still raises. Silently downgrading a malformed
    credential to anonymous would hide a broken client and, on an endpoint that
    reveals more to its owner, could quietly serve the public view instead.
    """
    if credentials is None or not credentials.credentials:
        return None
    return await _resolve_user(credentials.credentials, session, request)


def require_role(*roles: Role) -> Callable[[User], Awaitable[User]]:
    """Build a dependency admitting only the listed roles. ADMIN always passes."""
    permitted = frozenset(roles) | {Role.ADMIN}

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in permitted:
            raise ForbiddenError(
                code=ErrorCode.INSUFFICIENT_ROLE,
                message="your role does not permit this action",
                details={"required": sorted(str(role) for role in roles)},
            )
        return user

    return dependency


def require_self_or_admin(user_id: uuid.UUID, user: User) -> User:
    """Assert *user* is the subject or an admin. Raises 403 otherwise."""
    if user.id != user_id and user.role is not Role.ADMIN:
        raise ForbiddenError(
            code=ErrorCode.FORBIDDEN, message="you may only act on your own account"
        )
    return user


def client_ip(request: Request) -> str | None:
    """Best-effort client address. Only ever stored hashed."""
    return request.client.host if request.client else None
