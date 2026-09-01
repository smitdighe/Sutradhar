"""Which providers are usable right now.

Google is the only provider. Absent credentials make it *unavailable*, not
broken: the application boots, ``/readyz`` reports it as unconfigured rather
than down, and the endpoints answer 503 with a specific code. An optional
feature that nobody configured is not an outage, and a missing environment
variable must never be able to stop the service starting.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.auth.oauth.base import OAuthProviderClient
from app.auth.oauth.google import GoogleClient
from app.config import get_settings
from app.core.errors import ErrorCode, UnavailableError

__all__ = ["ProviderStatus", "get_provider", "provider_statuses"]

GOOGLE = "google"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """What the frontend needs to decide whether to render a button."""

    provider: str
    enabled: bool


def provider_statuses() -> list[ProviderStatus]:
    """Every known provider and whether it is usable."""
    return [ProviderStatus(provider=GOOGLE, enabled=get_settings().google_oauth_enabled)]


def get_provider(name: str) -> OAuthProviderClient:
    """Return a configured client, or raise 503.

    Read fresh on each call rather than built once at import: settings can be
    reloaded in tests, and a provider that was disabled at boot should become
    usable without a restart.
    """
    if name != GOOGLE or not get_settings().google_oauth_enabled:
        raise UnavailableError(
            code=ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
            message=f"{name} sign-in is not configured",
            details={"provider": name},
        )
    return GoogleClient()
