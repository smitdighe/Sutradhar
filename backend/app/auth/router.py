"""Auth endpoints.

Every response here carries ``Cache-Control: no-store``. These bodies contain
access tokens and profile data; a shared proxy or a browser back-button cache
holding one is a credential leak with no upside.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import client_ip, get_current_user
from app.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
)
from app.auth.service import (
    ClientContext,
    authenticate,
    record_auth_event,
    register,
    update_profile,
)
from app.auth.sessions import (
    IssuedRefreshToken,
    issue_family,
    revoke_all_for_user,
    revoke_family,
    rotate,
)
from app.auth.tokens import hash_refresh_token, issue_access_token
from app.config import get_settings
from app.core.errors import AuthError, ErrorCode
from app.core.ratelimit import consume
from app.db.models.enums import AuthEventType
from app.db.models.user import RefreshToken, User
from app.db.session import SessionLocal, get_session

__all__ = ["router"]


async def no_store(response: Response) -> None:
    """Applied to every route on this router."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(no_store)])


def _context(request: Request) -> ClientContext:
    return ClientContext(ip=client_ip(request), user_agent=request.headers.get("user-agent"))


def _cookie_path() -> str:
    """Scope the refresh cookie to the auth routes.

    Narrower would be wrong: ``/logout`` has to read the token it revokes, and
    clearing a cookie requires a matching Path. This still keeps it off every
    non-auth request, which is the point of scoping it at all.
    """
    return f"{get_settings().api_prefix}/auth"


def _set_refresh_cookie(response: Response, issued: IssuedRefreshToken) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=issued.raw,
        max_age=settings.refresh_token_ttl_seconds,
        path=_cookie_path(),
        httponly=True,  # unreadable from JavaScript, so XSS cannot exfiltrate it
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=_cookie_path(),
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )


def _presented_token(request: Request, body_token: str | None) -> str | None:
    """Cookie first, then the body fallback for non-browser clients."""
    return request.cookies.get(get_settings().refresh_cookie_name) or body_token


async def _issue_session(
    session: AsyncSession, response: Response, user_id: uuid.UUID, role: str, request: Request
) -> tuple[str, int]:
    issued = await issue_family(
        session, user_id, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    _set_refresh_cookie(response, issued)
    return issue_access_token(user_id, role)


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def register_account(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    """Create an account. Does not log the caller in."""
    settings = get_settings()
    if settings.rate_limit_enabled:
        await consume(
            SessionLocal,
            "register",
            client_ip(request) or "unknown",
            settings.rate_limit_register_per_hour,
            3600,
        )

    user = await register(session, payload, _context(request))
    await session.commit()
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Exchange credentials for an access token and a refresh cookie."""
    settings = get_settings()
    if settings.rate_limit_enabled:
        ip = client_ip(request) or "unknown"
        # Two limiters. The narrow one stops password-guessing against one
        # account; the looser per-IP one stops spraying one password across
        # many accounts, which the narrow limiter alone never notices.
        await consume(
            SessionLocal, "login_ip", ip, settings.rate_limit_login_ip_per_minute, 60
        )
        # Counted before the credentials are checked, so a wrong password costs
        # exactly what a right one does. A limiter that only counts failures is
        # a free oracle for confirming a correct password.
        await consume(
            SessionLocal,
            "login",
            f"{ip}|{str(payload.email).casefold()}",
            settings.rate_limit_login_per_minute,
            60,
        )

    user = await authenticate(session, payload, _context(request))
    access_token, expires_in = await _issue_session(
        session, response, user.id, str(user.role), request
    )
    await session.commit()
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Rotate the refresh token and mint a fresh access token."""
    settings = get_settings()
    presented = _presented_token(request, payload.refresh_token)
    if not presented:
        raise AuthError(
            code=ErrorCode.INVALID_REFRESH_TOKEN, message="refresh token is not valid"
        )

    if settings.rate_limit_enabled:
        # "Per user" needs the user, and the user is only known after lookup.
        # Keying on the token itself would be useless -- rotation issues a new
        # one each time, so every request would get a fresh bucket. One indexed
        # SELECT to find the owner, then limit on that.
        owner_id = (
            await session.execute(
                select(RefreshToken.user_id).where(
                    RefreshToken.token_hash == hash_refresh_token(presented)
                )
            )
        ).scalar_one_or_none()
        if owner_id is None:
            await consume(SessionLocal, "refresh_ip", client_ip(request) or "unknown", 60, 60)
        else:
            await consume(
                SessionLocal,
                "refresh",
                str(owner_id),
                settings.rate_limit_refresh_per_minute,
                60,
            )

    issued = await rotate(
        session,
        presented,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    account = await session.get(User, issued.record.user_id)
    if account is None:  # pragma: no cover - FK guarantees this
        raise AuthError(code=ErrorCode.INVALID_REFRESH_TOKEN, message="refresh token is not valid")

    _set_refresh_cookie(response, issued)
    access_token, expires_in = issue_access_token(account.id, str(account.role))
    await record_auth_event(session, AuthEventType.REFRESH, account.id, _context(request))
    await session.commit()
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserResponse.model_validate(account),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke the presented token's whole family and clear the cookie.

    Idempotent: an absent or unknown token still returns 204. Logout reporting
    an error is unhelpful -- the caller wanted to be logged out, and after this
    they are.
    """
    presented = _presented_token(request, payload.refresh_token)
    if presented:
        record = (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == hash_refresh_token(presented)
                )
            )
        ).scalar_one_or_none()
        if record is not None:
            await revoke_family(session, record.family_id)
            await record_auth_event(
                session, AuthEventType.LOGOUT, record.user_id, _context(request)
            )
            await session.commit()

    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke every family for the authenticated user."""
    await revoke_all_for_user(session, user.id)
    await record_auth_event(session, AuthEventType.LOGOUT, user.id, _context(request))
    await session.commit()

    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserResponse)
async def read_me(user: User = Depends(get_current_user)) -> UserResponse:
    """The authenticated user's own profile."""
    return UserResponse.model_validate(user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    """Update display name and region. No other field is reachable."""
    updated = await update_profile(session, user, payload)
    await session.commit()
    return UserResponse.model_validate(updated)
