"""Request and response models for the OAuth endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.auth.roles import Role

__all__ = ["CompleteRequest", "ProviderListResponse", "ProviderStatusResponse"]


class CompleteRequest(BaseModel):
    """Finish a new-identity sign-up.

    ``role`` is mandatory and is always re-validated against
    ``SELF_ASSIGNABLE_ROLES`` in the handler. The pending token deliberately
    carries no role, so this is the only place one enters the system and the
    only place it can be checked. Email is absent: it comes from the verified
    provider identity, never from the client.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    pending_token: str = Field(min_length=1)
    role: Role
    display_name: str = Field(min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    org_name: str | None = Field(default=None, max_length=200)


class ProviderStatusResponse(BaseModel):
    """One provider and whether it is usable right now."""

    provider: str
    enabled: bool


class ProviderListResponse(BaseModel):
    """Collection envelope, per app.core.envelope."""

    data: list[ProviderStatusResponse]
