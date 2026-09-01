"""Identity: accounts, linked OAuth identities, sessions, and the auth audit log."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import (
    AUTH_EVENT_TYPE,
    OAUTH_PROVIDER,
    USER_ROLE,
    USER_STATUS,
    AuthEventType,
    OAuthProvider,
    UserRole,
    UserStatus,
)
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["AuthEvent", "OAuthIdentity", "PendingToken", "RefreshToken", "User"]


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person or organisation with an account."""

    __tablename__ = "users"

    # citext so 'Weaver@example.com' and 'weaver@example.com' collide on the
    # unique index rather than becoming two accounts.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    # Null for OAuth-only accounts, which have no password to hash.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[UserRole] = mapped_column(
        USER_ROLE, nullable=False, default=UserRole.CONSUMER
    )
    status: Mapped[UserStatus] = mapped_column(
        USER_STATUS,
        nullable=False,
        default=UserStatus.PENDING_VERIFICATION,
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    org_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Opt-out from the public verification page's maker section. Default false
    # -- the story is most of the value of the record for the person who made
    # the object -- but it is theirs to withdraw, and withdrawing it removes the
    # display name and region from the public payload without touching the
    # provenance chain, which stays intact and still verifies.
    public_display_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The crypto-shredding salt. Deleting this row is the DPDP erasure action;
    # see app.core.crypto_shred for what that does to on-chain records.
    identity_salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fraud_flagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    oauth_identities: Mapped[list[OAuthIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_role", "role"),
        Index("ix_users_status", "status"),
        Index("ix_users_created_at_id", "created_at", "id"),
    )


class OAuthIdentity(UUIDPrimaryKeyMixin, Base):
    """A provider account linked to a :class:`User`."""

    __tablename__ = "oauth_identities"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[OAuthProvider] = mapped_column(
        OAUTH_PROVIDER, nullable=False
    )
    # The provider's immutable subject id -- never the email, which can change.
    provider_subject: Mapped[str] = mapped_column(Text, nullable=False)
    provider_email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="oauth_identities")

    __table_args__ = (
        # One account per provider subject, and one link per provider per user.
        UniqueConstraint(
            "provider", "provider_subject", name="uq_oauth_identities_provider_subject"
        ),
        UniqueConstraint("user_id", "provider", name="uq_oauth_identities_user_provider"),
        Index("ix_oauth_identities_user_id", "user_id"),
    )


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """One issued refresh token within a rotating family.

    ``family_id`` groups every token descended from one login. Presenting a
    token that has already been replaced means the token leaked, so the whole
    family is revoked at once rather than just that token.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # sha256 hex of the token; the token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
        Index("ix_refresh_tokens_replaced_by", "replaced_by"),
    )


class PendingToken(Base):
    """A single-use ticket issued mid-OAuth, before an account exists.

    Deliberately not a :class:`User` foreign key: this row is created when the
    provider has authenticated somebody the service has never seen. Single use
    is enforced by ``UPDATE ... WHERE consumed_at IS NULL``, so two concurrent
    completions cannot both win.
    """

    __tablename__ = "pending_tokens"

    jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[OAuthProvider] = mapped_column(
        OAUTH_PROVIDER, nullable=False
    )
    provider_subject: Mapped[str] = mapped_column(Text, nullable=False)
    provider_email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_pending_tokens_expires_at", "expires_at"),)


class AuthEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only authentication audit trail. Rows are never updated.

    ``user_id`` is nullable and set to null on user deletion: a failed login
    against a since-deleted account is still worth keeping, and the event
    carries no PII of its own -- IP and user agent are stored only as hashes.
    """

    __tablename__ = "auth_events"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[AuthEventType] = mapped_column(
        AUTH_EVENT_TYPE, nullable=False
    )
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_auth_events_user_id_created_at", "user_id", "created_at"),
        Index("ix_auth_events_event_type_created_at", "event_type", "created_at"),
    )
