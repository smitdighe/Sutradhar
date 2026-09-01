"""OAuth endpoints: start, callback, complete, and provider availability.

The callback never returns JSON. It is reached by a browser redirect from
Google, so every outcome -- success, refusal, malformed state -- ends as a 302
to a frontend URL. Provider-supplied error text is never propagated; only a
fixed, safe code.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import client_ip
from app.auth.linking import LinkOutcome, resolve_identity
from app.auth.oauth.base import new_pkce_pair
from app.auth.oauth.registry import GOOGLE, get_provider, provider_statuses
from app.auth.oauth.schemas import CompleteRequest, ProviderListResponse, ProviderStatusResponse
from app.auth.oauth.state import claim_nonce, issue_state, read_state
from app.auth.pending import burn_pending_token, read_pending_token
from app.auth.roles import Role, is_self_assignable
from app.auth.schemas import TokenResponse, UserResponse
from app.auth.service import ClientContext, record_auth_event
from app.auth.sessions import issue_family
from app.auth.tokens import issue_access_token
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.core.errors import AppError, ConflictError, ErrorCode, ForbiddenError
from app.core.logging import get_logger
from app.core.ratelimit import consume
from app.db.models.enums import AuthEventType, OAuthProvider, UserStatus
from app.db.models.user import OAuthIdentity, User
from app.db.session import SessionLocal, get_session

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _context(request: Request) -> ClientContext:
    return ClientContext(ip=client_ip(request), user_agent=request.headers.get("user-agent"))


def _error_redirect(code: str) -> RedirectResponse:
    """Send the browser to the frontend error page with a safe code.

    A fixed code, never the provider's message: that text is attacker-influenced
    in the general case and rendering it is a content-injection vector.
    """
    target = f"{get_settings().frontend_auth_error_url}?{urlencode({'error': code})}"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.get("/providers", response_model=ProviderListResponse)
async def list_providers(response: Response) -> ProviderListResponse:
    """Which providers are usable right now.

    Always 200, even when nothing is configured -- the frontend asks this in
    order to decide whether to render a button, and an error would be a worse
    answer than ``enabled: false``.
    """
    await _no_store(response)
    return ProviderListResponse(
        data=[
            ProviderStatusResponse(provider=item.provider, enabled=item.enabled)
            for item in provider_statuses()
        ]
    )


@router.get("/google/start")
async def start_google(request: Request, return_to: str | None = None) -> RedirectResponse:
    """Begin the authorization-code flow. 302 to Google, or 503 if unconfigured."""
    settings = get_settings()
    if settings.rate_limit_enabled:
        await consume(
            SessionLocal,
            "oauth_start",
            client_ip(request) or "unknown",
            settings.rate_limit_oauth_start_per_minute,
            60,
        )

    # Raises 503 OAUTH_PROVIDER_UNAVAILABLE when credentials are absent. Never
    # a 500, and never a failure to boot.
    provider = get_provider(GOOGLE)

    pkce = new_pkce_pair()
    state, _nonce = issue_state(pkce.verifier, return_to)
    redirect = RedirectResponse(
        provider.authorization_url(state, pkce), status_code=status.HTTP_302_FOUND
    )
    redirect.headers["Cache-Control"] = "no-store"
    return redirect


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Handle Google's redirect back.

    An identity that already maps to an account gets a full session here. One
    that does not gets a pending token and **no session at all** -- no access
    token in the redirect, no refresh cookie set. See :mod:`app.auth.pending`.
    """
    settings = get_settings()

    # The user pressed "cancel", or Google refused. Not an error condition on
    # this side; just send them back.
    if error:
        return _error_redirect("provider_denied")

    if not get_settings().google_oauth_enabled:
        raise AppError(
            code=ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
            message="google sign-in is not configured",
            status=503,
        )

    if not code or not state:
        return _error_redirect("invalid_request")

    # Signature and age first, then spend the nonce. Both must pass before the
    # code is exchanged, so a replayed callback never reaches Google.
    payload = read_state(state)
    await claim_nonce(session, payload.nonce)

    provider = get_provider(GOOGLE)
    identity = await provider.exchange(code, payload.pkce_verifier)

    resolution = await resolve_identity(session, identity)

    if resolution.outcome is LinkOutcome.NEW_IDENTITY:
        from app.auth.pending import issue_pending_token

        pending = await issue_pending_token(session, identity)
        await session.commit()
        target = (
            f"{settings.frontend_completion_url}?{urlencode({'pending_token': pending})}"
        )
        redirect = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
        # Deliberately no cookie and no token: this browser is not authenticated
        # and will not be until /complete supplies a role.
        redirect.headers["Cache-Control"] = "no-store"
        return redirect

    user = resolution.user
    assert user is not None  # noqa: S101 - guaranteed by LinkOutcome above

    if user.status is UserStatus.SUSPENDED:
        await session.commit()
        return _error_redirect("account_suspended")

    issued = await issue_family(
        session,
        user.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await record_auth_event(session, AuthEventType.LOGIN_SUCCESS, user.id, _context(request))
    await session.commit()

    destination = payload.return_to or settings.frontend_post_login_url
    redirect = RedirectResponse(destination, status_code=status.HTTP_302_FOUND)
    redirect.set_cookie(
        key=settings.refresh_cookie_name,
        value=issued.raw,
        max_age=settings.refresh_token_ttl_seconds,
        path=f"{settings.api_prefix}/auth",
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )
    redirect.headers["Cache-Control"] = "no-store"
    return redirect


@router.post("/complete", response_model=TokenResponse)
async def complete(
    payload: CompleteRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Finish a new-identity sign-up: supply a role, get an account and a session."""
    settings = get_settings()
    await _no_store(response)

    claims = read_pending_token(payload.pending_token)

    if settings.rate_limit_enabled:
        # Keyed on the jti: the limit belongs to this one sign-up attempt, not
        # to an IP that may be shared by a whole co-operative.
        await consume(
            SessionLocal,
            "oauth_complete",
            str(claims.jti),
            settings.rate_limit_complete_per_minute,
            60,
        )

    # Never trust a client-supplied role. The pending token carries none, which
    # is why it has to be checked here and cannot be smuggled in the token.
    role = payload.role
    if not is_self_assignable(role):
        raise ForbiddenError(
            code=ErrorCode.ROLE_NOT_SELF_ASSIGNABLE,
            message=f"role {role} must be granted by an administrator",
            details={"role": str(role)},
        )

    # Atomic single use. Two concurrent completions: exactly one claims it.
    record = await burn_pending_token(session, claims.jti)

    # A second identity may have completed between mint and burn. The unique
    # index on (provider, provider_subject) is the real enforcement; this is
    # the friendly path to the same answer.
    already = (
        await session.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == OAuthProvider.GOOGLE,
                OAuthIdentity.provider_subject == record.provider_subject,
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise ConflictError(
            code=ErrorCode.OAUTH_IDENTITY_LINKED,
            message="this provider account is already linked to a user",
        )

    # Same rule as Phase 2 registration: a self-declared weaver is not a
    # trusted weaver until a human verifies the claim.
    account_status = (
        UserStatus.PENDING_VERIFICATION if role is Role.WEAVER else UserStatus.ACTIVE
    )

    user = User(
        email=str(record.provider_email),
        password_hash=None,  # OAuth-only account; no password exists to verify
        display_name=payload.display_name,
        role=role,
        status=account_status,
        region=payload.region,
        org_name=payload.org_name,
        identity_salt=new_salt(),
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            code=ErrorCode.EMAIL_ALREADY_REGISTERED,
            message="an account with this email already exists",
        ) from exc

    session.add(
        OAuthIdentity(
            user_id=user.id,
            provider=OAuthProvider.GOOGLE,
            provider_subject=record.provider_subject,
            provider_email=record.provider_email,
            email_verified=True,
        )
    )
    await record_auth_event(
        session,
        AuthEventType.OAUTH_NEW_ACCOUNT,
        user.id,
        _context(request),
        {"provider": str(OAuthProvider.GOOGLE), "role": str(role)},
    )

    issued = await issue_family(
        session, user.id, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    await session.commit()

    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=issued.raw,
        max_age=settings.refresh_token_ttl_seconds,
        path=f"{settings.api_prefix}/auth",
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )
    access_token, expires_in = issue_access_token(user.id, str(user.role))
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )
