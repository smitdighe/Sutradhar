"""Provider-agnostic shapes for the authorization-code flow.

Google is the only provider and is intended to stay that way. These types exist
not to make adding providers easy, but to keep the router free of Google's
specific JSON shapes -- the router deals in :class:`ProviderIdentity`, and
whether that came from an ID token or somewhere else is the provider's problem.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "PKCEPair",
    "OAuthProviderClient",
    "ProviderIdentity",
    "new_pkce_pair",
]


@dataclass(frozen=True, slots=True)
class PKCEPair:
    """A PKCE verifier and its S256 challenge.

    PKCE binds the authorization code to the client that requested it. Without
    it, anybody who intercepts the code -- from a redirect logged by a proxy, a
    browser history entry, a misconfigured referrer -- can exchange it. The
    verifier never leaves this server; only its hash goes to the provider.
    """

    verifier: str
    challenge: str
    method: str = "S256"


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """A verified identity asserted by a provider.

    ``subject`` is the provider's immutable identifier for the account. It is
    the only field safe to key on -- see :mod:`app.auth.linking`.

    ``email_verified`` is carried explicitly and is never assumed. An
    unverified provider email is an account-takeover primitive: anybody who can
    register that address at the provider could otherwise claim the local
    account that already owns it.
    """

    provider: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None = None


def new_pkce_pair() -> PKCEPair:
    """Generate a fresh PKCE verifier and its S256 challenge."""
    # 32 raw bytes -> 43 base64url characters, comfortably inside RFC 7636's
    # 43..128 range.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PKCEPair(verifier=verifier, challenge=challenge)


class OAuthProviderClient(Protocol):
    """What the router needs from a provider."""

    name: str

    def authorization_url(self, state: str, pkce: PKCEPair) -> str:
        """Build the URL to redirect the browser to."""
        ...

    async def exchange(self, code: str, verifier: str) -> ProviderIdentity:
        """Trade an authorization code for a verified identity."""
        ...
