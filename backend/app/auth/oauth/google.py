"""Google as an OAuth 2.0 / OpenID Connect authorization-code client.

The identity comes from the **ID token**, verified against Google's published
JWKS. Two things this deliberately does not do:

* It does not trust the userinfo endpoint on its own. Userinfo is an ordinary
  bearer-authenticated JSON endpoint -- anything that can reach it with a token
  gets an answer, and the response carries no proof of who issued it.
* It does not decode the ID token without verifying it. An unverified JWT is a
  base64 blob supplied by whoever redirected the browser here.

Verification pins the signature, the issuer, the audience, and the expiry. The
audience check is what stops a token minted for a *different* Google client from
being replayed at this one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt.exceptions import InvalidTokenError

from app.auth.oauth.base import PKCEPair, ProviderIdentity
from app.config import get_settings
from app.core.errors import ErrorCode, UnavailableError, ValidationError

__all__ = [
    "GOOGLE_ISSUERS",
    "JWKS_CACHE_TTL_SECONDS",
    "GoogleClient",
    "reset_jwks_cache",
]

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

# Google mints both spellings and has done for years. Both are legitimate.
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

SCOPES = "openid email profile"
JWKS_CACHE_TTL_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 10.0


@dataclass
class _JWKSCache:
    """Google's signing keys, refetched on TTL.

    Cached because the callback sits on the login path and Google's keys rotate
    on the order of days -- fetching them per request would be pure latency.

    Fetched with httpx rather than :class:`jwt.PyJWKClient`, which uses urllib.
    One HTTP stack means one timeout policy, and it keeps the fetch inside the
    same transport the rest of the service uses.
    """

    keys: dict[str, Any] | None = None
    fetched_at: float = 0.0

    async def fetch(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(JWKS_URI)
            response.raise_for_status()
            document: dict[str, Any] = response.json()
        self.keys = document
        self.fetched_at = time.monotonic()
        return document

    async def get(self) -> dict[str, Any]:
        if self.keys is None or (time.monotonic() - self.fetched_at) > JWKS_CACHE_TTL_SECONDS:
            return await self.fetch()
        return self.keys


_jwks_cache = _JWKSCache()


def reset_jwks_cache() -> None:
    """Drop the cached JWKS. For tests and key-rotation incidents."""
    _jwks_cache.keys = None
    _jwks_cache.fetched_at = 0.0


async def _signing_key(id_token: str) -> Any:
    """Find the key that signed *id_token*, refetching once on a miss.

    An unknown ``kid`` usually means Google rotated keys since the last fetch,
    so one forced refresh is worth it before giving up.
    """
    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")

    document = await _jwks_cache.get()
    for candidate in document.get("keys", []):
        if candidate.get("kid") == kid:
            return jwt.PyJWK(candidate, algorithm=candidate.get("alg", "RS256")).key

    document = await _jwks_cache.fetch()
    for candidate in document.get("keys", []):
        if candidate.get("kid") == kid:
            return jwt.PyJWK(candidate, algorithm=candidate.get("alg", "RS256")).key

    raise InvalidTokenError(f"no signing key matches kid {kid!r}")


class GoogleClient:
    """Authorization-code client for Google, with PKCE."""

    name = "google"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.google_oauth_enabled:
            # Constructed only via the registry, which checks first. Belt and
            # braces so a direct construction cannot half-work.
            raise UnavailableError(
                code=ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
                message="google sign-in is not configured",
            )
        self._client_id = settings.google_client_id
        self._client_secret = settings.google_client_secret
        self._redirect_uri = settings.google_redirect_uri

    def authorization_url(self, state: str, pkce: PKCEPair) -> str:
        """Build Google's consent URL."""
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
                "code_challenge": pkce.challenge,
                "code_challenge_method": pkce.method,
                "access_type": "online",
                # Force the account chooser rather than silently reusing a
                # session -- on a shared device the previous user's account
                # would otherwise be linked without anybody noticing.
                "prompt": "select_account",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    async def exchange(self, code: str, verifier: str) -> ProviderIdentity:
        """Trade an authorization code for a verified identity."""
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    TOKEN_ENDPOINT,
                    data={
                        "code": code,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": self._redirect_uri,
                        "grant_type": "authorization_code",
                        "code_verifier": verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise UnavailableError(
                    code=ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
                    message="could not reach the identity provider",
                ) from exc

        if response.status_code != 200:
            # Google's error text is provider-controlled and is never surfaced
            # to the caller or rendered anywhere.
            raise ValidationError(
                code=ErrorCode.OAUTH_STATE_INVALID,
                status=400,
                message="authorization code could not be exchanged",
            )

        payload = response.json()
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ValidationError(
                code=ErrorCode.OAUTH_STATE_INVALID,
                status=400,
                message="identity provider returned no id token",
            )
        return await self.verify_id_token(id_token)

    async def verify_id_token(self, id_token: str) -> ProviderIdentity:
        """Verify an ID token's signature and claims, then read the identity."""
        invalid = ValidationError(
            code=ErrorCode.OAUTH_STATE_INVALID,
            status=400,
            message="identity provider token could not be verified",
        )
        try:
            signing_key = await _signing_key(id_token)
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=self._client_id,
                options={
                    "require": ["sub", "aud", "exp", "iat", "iss"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_nbf": True,
                },
            )
        except (InvalidTokenError, httpx.HTTPError, ValueError, KeyError) as exc:
            raise invalid from exc

        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise invalid

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise invalid

        email = claims.get("email")
        return ProviderIdentity(
            provider="GOOGLE",
            subject=subject,
            email=email if isinstance(email, str) and email else None,
            # Google sends this as a real bool, but has historically sent the
            # strings "true"/"false" through some paths. Anything that is not
            # an affirmative is treated as unverified.
            email_verified=claims.get("email_verified") in (True, "true"),
            display_name=claims.get("name") if isinstance(claims.get("name"), str) else None,
        )
