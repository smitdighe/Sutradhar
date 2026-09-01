"""Request and response models for the auth endpoints.

The profile-update model is the security-relevant one: it declares exactly two
fields and drops everything else. ``role``, ``status`` and ``email`` are not
optional-and-ignored, they are absent from the schema, so there is no code path
that could assign them however the request is shaped.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.auth.roles import Role
from app.core.clock import UtcDatetime
from app.db.models.enums import UserStatus

__all__ = [
    "LoginRequest",
    "LogoutRequest",
    "RefreshRequest",
    "RegisterRequest",
    "TokenResponse",
    "UpdateProfileRequest",
    "UserResponse",
]


class RegisterRequest(BaseModel):
    """New account. ``role`` must be self-assignable; absent means CONSUMER."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    role: Role | None = None
    region: str | None = Field(default=None, max_length=120)
    org_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    """Body fallback for clients without a cookie jar."""

    model_config = ConfigDict(extra="ignore")

    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    refresh_token: str | None = None


class UserResponse(BaseModel):
    """Public shape of a user. No password hash, no identity salt, ever."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: Role
    status: UserStatus
    region: str | None = None
    org_name: str | None = None
    created_at: UtcDatetime
    last_login_at: UtcDatetime | None = None


class TokenResponse(BaseModel):
    """Login and refresh response. The refresh token rides in the cookie."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class UpdateProfileRequest(BaseModel):
    """The only two fields a user may change about themselves.

    ``extra="ignore"`` plus the absence of role/status/email means a request
    carrying them is accepted and those fields are silently dropped -- there is
    no branch that could apply them. Escalation via PATCH is structurally
    impossible rather than defended against.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=120)
