"""Python enums mirrored one-to-one by native Postgres enum types.

Native enum types rather than check constraints or text: the database rejects an
unknown value at write time, and the type name is visible in the schema, which
makes an unintended value impossible to introduce from a psql session.

Adding a member is a migration (``ALTER TYPE ... ADD VALUE``). Removing or
renaming one is a breaking change to stored rows -- treat these as append-only.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum

__all__ = [
    "ALL_ENUM_TYPES",
    "ALL_ENUM_TYPE_NAMES",
    "AuthEventType",
    "ChainTxStatus",
    "DisputeSource",
    "DisputeStatus",
    "ItemEventType",
    "ItemStatus",
    "MediaKind",
    "OAuthProvider",
    "OutboxJobType",
    "OutboxStatus",
    "PinStatus",
    "SuspicionLevel",
    "UserRole",
    "UserStatus",
    "pg_enum",
]


class UserRole(StrEnum):
    """Who someone is in the supply chain. One role per user."""

    CONSUMER = "CONSUMER"
    WEAVER = "WEAVER"
    COOP_OFFICER = "COOP_OFFICER"
    INSPECTOR = "INSPECTOR"
    ADMIN = "ADMIN"


class UserStatus(StrEnum):
    """Account lifecycle state."""

    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class OAuthProvider(StrEnum):
    """Supported OAuth providers.

    Google is the only member and is intended to stay that way.
    """

    GOOGLE = "GOOGLE"


class AuthEventType(StrEnum):
    """Append-only authentication audit events."""

    REGISTER = "REGISTER"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    REFRESH = "REFRESH"
    REFRESH_REUSE_DETECTED = "REFRESH_REUSE_DETECTED"
    LOGOUT = "LOGOUT"
    OAUTH_LINK = "OAUTH_LINK"
    OAUTH_NEW_ACCOUNT = "OAUTH_NEW_ACCOUNT"
    ROLE_GRANT = "ROLE_GRANT"
    FRAUD_FLAG = "FRAUD_FLAG"
    # Reversal of a flag. Its own member rather than a field on FRAUD_FLAG: the
    # audit trail has to read as two events, not as one event that changed its
    # mind, because an append-only log cannot un-say the first one.
    FRAUD_CLEAR = "FRAUD_CLEAR"


class ItemStatus(StrEnum):
    """Anchoring state of an item's hash. PENDING until the chain confirms."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class DisputeStatus(StrEnum):
    """Whether an item's provenance is contested."""

    NONE = "NONE"
    DISPUTED = "DISPUTED"


class DisputeSource(StrEnum):
    """Why an item is disputed. Several may apply to one item at once.

    The source is what makes a dispute reversible without collateral damage:
    clearing a fraud flag lifts the disputes that flag caused and leaves an
    inspector's independent finding standing. A single boolean on the item could
    not tell the two apart, so lifting either would silently erase the other.
    """

    # Raised by fraud-flagging the actor who registered the item.
    FRAUD_FLAG = "FRAUD_FLAG"
    # Raised by an inspector recording a finding against the item itself.
    INSPECTION = "INSPECTION"
    # Raised by an admin for a reason outside the other two.
    MANUAL = "MANUAL"


class ItemEventType(StrEnum):
    """Append-only provenance events on an item."""

    REGISTERED = "REGISTERED"
    SPLIT = "SPLIT"
    ATTESTED = "ATTESTED"
    ANCHORED = "ANCHORED"
    DISPUTED = "DISPUTED"
    CLAIMED = "CLAIMED"
    # A physical tag was bound to this item. Its own event rather than a column
    # timestamp: the tag is the object's public identity, and when it was issued
    # and by whom is exactly the sort of thing a dispute turns on.
    TAG_ISSUED = "TAG_ISSUED"
    # A reorg dropped the block that carried this item's anchor, so the item
    # went back to PENDING. Its own event type rather than a flag inside an
    # ANCHORED payload: an item that was anchored and then un-anchored is a
    # different history from one that was simply anchored, and a reader
    # scanning event types should not have to open payloads to tell them apart.
    REORGED = "REORGED"
    # The anchoring transaction reverted, or the job exhausted its retries.
    ANCHOR_FAILED = "ANCHOR_FAILED"
    # A dispute was lifted. Recorded because a dispute that appears and then
    # vanishes with no trace is indistinguishable from one that never happened,
    # and a consumer who saw the dispute deserves to see its resolution.
    DISPUTE_CLEARED = "DISPUTE_CLEARED"


class PinStatus(StrEnum):
    """IPFS pinning state for a media blob."""

    PIN_PENDING = "PIN_PENDING"
    PINNED = "PINNED"
    PIN_FAILED = "PIN_FAILED"


class MediaKind(StrEnum):
    """What a piece of media depicts, per item."""

    LOOM_PHOTO = "LOOM_PHOTO"
    WEAVE_MACRO = "WEAVE_MACRO"
    CERTIFICATE = "CERTIFICATE"
    VIDEO = "VIDEO"


class SuspicionLevel(StrEnum):
    """Anomaly verdict recorded against a single scan."""

    NONE = "NONE"
    WATCH = "WATCH"
    SUSPICIOUS = "SUSPICIOUS"


class OutboxJobType(StrEnum):
    """What a durable background job is asked to do.

    The outbox began as a chain-anchoring queue and is now the one mechanism for
    any work that must survive a crash: claimed under ``SKIP LOCKED``, retried
    with backoff, and dead-lettered with its full error history. Members are
    dispatched by separate drains, so a job type only ever reaches the worker
    that understands it.
    """

    ANCHOR_ITEM = "ANCHOR_ITEM"
    ANCHOR_ATTESTATION = "ANCHOR_ATTESTATION"
    ANCHOR_BATCH = "ANCHOR_BATCH"
    # Pinning is a network call to a third party that can be down for hours.
    # Retrying it needs exactly the durable-job machinery anchoring already has,
    # and a second copy of that machinery would drift from the first.
    PIN_MEDIA = "PIN_MEDIA"


class OutboxStatus(StrEnum):
    """Outbox row lifecycle. DEAD rows are moved to ``dead_letters``."""

    QUEUED = "QUEUED"
    IN_FLIGHT = "IN_FLIGHT"
    DONE = "DONE"
    DEAD = "DEAD"


class ChainTxStatus(StrEnum):
    """Transaction lifecycle. ORPHANED means a reorg dropped a mined tx."""

    SENT = "SENT"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    ORPHANED = "ORPHANED"
    FAILED = "FAILED"


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Bind a Python enum to a native Postgres enum type called *name*.

    ``values_callable`` makes Postgres store the member *value* rather than the
    member name. They are identical here, but stating it means a future rename
    of a member cannot silently change what is written to the database.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_type=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


# One shared instance per type. A named Postgres enum used by two tables must be
# the *same* object, or SQLAlchemy emits CREATE TYPE twice and the second fails.
USER_ROLE = pg_enum(UserRole, "user_role")
USER_STATUS = pg_enum(UserStatus, "user_status")
OAUTH_PROVIDER = pg_enum(OAuthProvider, "oauth_provider")
AUTH_EVENT_TYPE = pg_enum(AuthEventType, "auth_event_type")
ITEM_STATUS = pg_enum(ItemStatus, "item_status")
DISPUTE_STATUS = pg_enum(DisputeStatus, "dispute_status")
DISPUTE_SOURCE = pg_enum(DisputeSource, "dispute_source")
ITEM_EVENT_TYPE = pg_enum(ItemEventType, "item_event_type")
PIN_STATUS = pg_enum(PinStatus, "pin_status")
MEDIA_KIND = pg_enum(MediaKind, "media_kind")
SUSPICION_LEVEL = pg_enum(SuspicionLevel, "suspicion_level")
OUTBOX_JOB_TYPE = pg_enum(OutboxJobType, "outbox_job_type")
OUTBOX_STATUS = pg_enum(OutboxStatus, "outbox_status")
CHAIN_TX_STATUS = pg_enum(ChainTxStatus, "chain_tx_status")

# Creation order for the migration: every type must exist before any table
# references it, and every one must be dropped on downgrade.
ALL_ENUM_TYPES = (
    USER_ROLE,
    USER_STATUS,
    OAUTH_PROVIDER,
    AUTH_EVENT_TYPE,
    ITEM_STATUS,
    DISPUTE_STATUS,
    DISPUTE_SOURCE,
    ITEM_EVENT_TYPE,
    PIN_STATUS,
    MEDIA_KIND,
    SUSPICION_LEVEL,
    OUTBOX_JOB_TYPE,
    OUTBOX_STATUS,
    CHAIN_TX_STATUS,
)

ALL_ENUM_TYPE_NAMES = tuple(enum_type.name for enum_type in ALL_ENUM_TYPES)
