"""Recording attestations and queueing them for anchoring.

**Everything in one transaction.** The attestation row, the provenance event and
the chain-outbox entry commit together or not at all -- the same rule Phase 6
applies to registration, for the same reason. An attestation with no outbox row
never gets anchored and looks permanently unrecorded; an outbox row with no
attestation anchors the hash of nothing.

**One chain path, not two.** Attestations go through the Phase 7 outbox and the
Phase 7 writer. There is no second sender, no second nonce source and no second
confirmation sweep, because two independent things allocating nonces for one key
is exactly how one anchor silently replaces another.

**The duplicate check is the database's, not this module's.** A second
attestation by the same actor on the same item is rejected by the
``uq_attestations_item_attestor`` constraint. An application-level "does one
already exist" read would be a check and a write with a gap between them, and
two simultaneous requests would both pass the check.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.attestation.statement_hash import attestor_hash, hash_statement
from app.chain.outbox import enqueue_job
from app.core.clock import now
from app.core.errors import ConflictError, ErrorCode, ForbiddenError, NotFoundError
from app.core.hashing import hash_object
from app.core.pagination import Cursor
from app.db.models.attestation import Attestation
from app.db.models.catalog import Item, ItemEvent
from app.db.models.enums import ItemEventType, OutboxJobType, UserRole
from app.db.models.user import User

__all__ = [
    "ATTESTING_ROLES",
    "AttestationView",
    "count_attestations",
    "create_attestation",
    "list_attestations",
    "load_item",
]

# Who may vouch for anything at all. A consumer scanning a tag has no standing
# to make a claim about how a textile was made.
ATTESTING_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.WEAVER, UserRole.COOP_OFFICER, UserRole.INSPECTOR, UserRole.ADMIN}
)


async def load_item(session: AsyncSession, item_id: uuid.UUID) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(
            code=ErrorCode.ITEM_NOT_FOUND, message=f"no item with id {item_id}"
        )
    return item


def _assert_may_attest(actor: User, item: Item) -> None:
    """Gate the write. Two refusals, and one thing deliberately *not* refused.

    A fraud-flagged actor is refused outright: the system has already decided it
    does not stand behind their claims, and recording a new one would put
    something in front of a reader that this service does not itself credit.

    A role with no standing to make claims about textiles is refused. A consumer
    scanning a tag is not a witness to how it was woven.

    **An account still awaiting verification is not refused.** It may record an
    attestation about anything, and that attestation simply does not raise
    anyone's level -- :meth:`app.attestation.trust.AttestorView.counts_toward_level`
    declines to count it. Refusing the write instead would throw away real
    evidence: an unverified officer's statement is still a statement somebody
    made, and it belongs in the record even though nothing should be inferred
    from it yet. Independence is decided in one place, by the trust computation,
    rather than half here and half there.

    Suspended accounts never reach this function: ``app.auth.guards`` rejects
    their tokens with a 401 before any route body runs.
    """
    if actor.fraud_flagged_at is not None:
        raise ForbiddenError(
            code=ErrorCode.ACTOR_FRAUD_FLAGGED,
            message="a fraud-flagged actor may not record attestations",
        )

    if actor.role not in ATTESTING_ROLES:
        raise ForbiddenError(
            code=ErrorCode.INSUFFICIENT_ROLE,
            message="your role does not permit recording an attestation",
            details={"required": sorted(str(role) for role in ATTESTING_ROLES)},
        )


async def create_attestation(
    session: AsyncSession,
    item_id: uuid.UUID,
    statement: dict[str, Any],
    actor: User,
) -> Attestation:
    """Record one attestation and queue it for anchoring. Caller commits."""
    item = await load_item(session, item_id)
    _assert_may_attest(actor, item)

    attested_at = now()
    statement_hash, preimage = hash_statement(
        item_hash=item.item_hash,
        attestor_id=actor.id,
        identity_salt=actor.identity_salt,
        # Snapshotted, not read live at display time. An inspector who later
        # becomes a consumer still made this attestation as an inspector.
        attestor_role=actor.role,
        statement=statement,
        attested_at=attested_at,
    )

    attestation = Attestation(
        item_id=item.id,
        attestor_id=actor.id,
        attestor_role=actor.role,
        statement=statement,
        statement_hash=statement_hash,
        created_at=attested_at,
    )
    session.add(attestation)

    try:
        # Forces the unique constraint now, so the conflict surfaces here as a
        # 409 rather than at commit time as an opaque 500 from the handler.
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            code=ErrorCode.DUPLICATE_ATTESTATION,
            message="this actor has already attested to this item",
            details={"item_id": str(item_id)},
        ) from exc

    event_payload: dict[str, Any] = {
        "preimage": preimage,
        "statement_hash": statement_hash,
        "attestor_role": str(actor.role),
    }
    session.add(
        ItemEvent(
            item_id=item.id,
            event_type=ItemEventType.ATTESTED,
            actor_id=actor.id,
            payload=event_payload,
            payload_hash=hash_object(event_payload),
        )
    )

    await enqueue_job(
        session,
        job_type=OutboxJobType.ANCHOR_ATTESTATION,
        payload={
            "attestation_id": str(attestation.id),
            "item_id": str(item.id),
            "statement_hash": statement_hash,
            "issuer_hash": str(preimage["attestor_hash"]),
        },
        # The statement hash is the natural idempotency key: the same actor
        # making the same claim about the same item at the same instant is one
        # anchor, not two.
        dedupe_key=statement_hash,
    )

    return attestation


@dataclass(frozen=True, slots=True)
class AttestationView:
    """One attestation, projected down to what a reader may see.

    No name, no email, no user id. ``attestor_ref`` is the salted identity
    digest -- the same value anchored on chain, so it is stable, verifiable
    against chain data, and correlatable across items by anyone who cares to
    notice that one inspector vouched for several pieces. It identifies nobody
    without the per-subject salt, and deleting that salt is the DPDP erasure
    action described in :mod:`app.core.crypto_shred`.
    """

    id: uuid.UUID
    item_id: uuid.UUID
    attestor_ref: str
    attestor_role: UserRole
    attestor_fraud_flagged: bool
    statement: dict[str, Any]
    statement_hash: str
    created_at: datetime


def _attestation_page(item_id: uuid.UUID, cursor: Cursor | None, limit: int) -> Select[Any]:
    statement = (
        select(Attestation, User.fraud_flagged_at, User.identity_salt)
        .join(User, User.id == Attestation.attestor_id)
        .where(Attestation.item_id == item_id)
        .order_by(Attestation.created_at.desc(), Attestation.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        statement = statement.where(
            tuple_(Attestation.created_at, Attestation.id) < (cursor.key, cursor.id)
        )
    return statement


async def list_attestations(
    session: AsyncSession,
    item_id: uuid.UUID,
    cursor: Cursor | None,
    limit: int,
) -> tuple[list[AttestationView], bool]:
    """One page of attestations, newest first. Returns ``(views, has_more)``.

    Each view carries the role held at attestation time and whether the attestor
    is *currently* fraud-flagged -- enough for a reader to weigh the claim, and
    nothing that identifies the person who made it.
    """
    rows = (await session.execute(_attestation_page(item_id, cursor, limit))).all()
    has_more = len(rows) > limit

    views: list[AttestationView] = []
    for attestation, flagged_at, salt in rows[:limit]:
        views.append(
            AttestationView(
                id=attestation.id,
                item_id=attestation.item_id,
                attestor_ref=attestor_hash(attestation.attestor_id, salt),
                attestor_role=attestation.attestor_role,
                attestor_fraud_flagged=flagged_at is not None,
                statement=dict(attestation.statement),
                statement_hash=attestation.statement_hash,
                created_at=attestation.created_at,
            )
        )
    return views, has_more


async def count_attestations(session: AsyncSession, item_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(
                select(Attestation.id).where(Attestation.item_id == item_id).subquery()
            )
        )
        or 0
    )
