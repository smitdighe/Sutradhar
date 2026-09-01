"""Fraud flags and how they propagate through everything an actor touched.

A flag is not a note on a profile. If a co-op officer turns out to have been
signing off on powerloom goods, every record they registered is in question and
every record they vouched for loses that vouching. Leaving those records looking
untouched while an admin knows better is the system telling a consumer something
it does not believe.

**Two different propagations, and only one of them writes.**

*Items the actor registered* become ``DISPUTED``. Their provenance is directly in
question, so the dispute is recorded as state -- an ``item_disputes`` row per
item, stamped with the actor whose flag caused it, so the reversal can be exact.

*Items the actor merely attested to* are not disputed and nothing is written for
them. Their trust level simply drops, because
:mod:`app.attestation.trust` recomputes from the attestation set on every read
and a flagged attestor stops counting. This is the derived-trust design paying
for itself: there is no stored level to backfill, no cache to invalidate, and no
window in which a flagged officer's endorsement is still being displayed. The
next read is already correct.

**Set-based, always.** An actor with ten thousand items is flagged with a fixed
number of statements -- none of them per-row. A loop here would take a
five-figure number of round trips, and the operation would be abandoned halfway
by whoever ran it, leaving half a fraud flag applied.

**Reversal is exact, not wholesale.** Clearing a flag lifts the disputes *that
flag* raised, identified by ``item_disputes.triggered_by``. An item an inspector
independently disputed stays disputed, and its dispute row is untouched. An item
is only returned to ``NONE`` when no open dispute of any source remains against
it -- checked in SQL, so two admins acting at once cannot both conclude the item
is clear.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, cast, exists, func, literal, not_, null, select, update
from sqlalchemy.dialects.postgresql import JSONB, Insert, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.errors import ErrorCode, NotFoundError
from app.core.hashing import hash_object
from app.core.logging import get_logger
from app.db.models.attestation import Attestation, ItemDispute
from app.db.models.catalog import Item, ItemEvent
from app.db.models.enums import (
    AuthEventType,
    DisputeSource,
    DisputeStatus,
    ItemEventType,
)
from app.db.models.user import AuthEvent, User

__all__ = [
    "FlagOutcome",
    "clear_fraud_flag",
    "flag_actor",
    "open_dispute_for",
    "raise_dispute",
]

logger = get_logger(__name__)


def _rows(result: Any) -> int:
    """Row count of a DML result.

    ``rowcount`` is declared on ``CursorResult`` rather than on the ``Result``
    the async session is typed to return, so it is read defensively instead of
    asserted -- a wrong count here would only mis-report a log field, and
    crashing a fraud flag over it would be worse than the imprecision.
    """
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(frozen=True, slots=True)
class FlagOutcome:
    """What one flag or clear actually changed."""

    actor_id: uuid.UUID
    items_affected: int
    attestations_affected: int
    already_in_state: bool

    def as_log_fields(self) -> dict[str, object]:
        return {
            "actor_id": str(self.actor_id),
            "items_affected": self.items_affected,
            "attestations_affected": self.attestations_affected,
            "already_in_state": self.already_in_state,
        }


async def _load_actor(session: AsyncSession, actor_id: uuid.UUID) -> User:
    actor = await session.get(User, actor_id)
    if actor is None:
        raise NotFoundError(
            code=ErrorCode.USER_NOT_FOUND, message=f"no user with id {actor_id}"
        )
    return actor


def _bulk_item_events(
    item_select: Select[tuple[uuid.UUID]],
    event_type: ItemEventType,
    payload: dict[str, object],
) -> Insert:
    """An ``INSERT ... SELECT`` writing one identical event per selected item.

    Every row carries the same payload, so the hash is computed once in Python
    and the whole insert stays a single statement no matter how many items it
    covers. That is the property the ten-thousand-item case depends on.

    ``gen_random_uuid()`` rather than the model's UUIDv7 default: the default is
    a Python callable and cannot run inside a set-based insert without a round
    trip per row, which is exactly what this statement exists to avoid. These
    ids are opaque and nothing orders by them.

    ``actor_id`` is null on purpose. A bulk dispute is not an act by a person
    against this particular item; the admin who flagged the actor is recorded
    once, on the ``auth_events`` row and on every ``item_disputes`` row.
    """
    payload_hash = hash_object(payload)
    moment = now()
    return pg_insert(ItemEvent).from_select(
        ["id", "item_id", "event_type", "actor_id", "payload", "payload_hash", "created_at"],
        item_select.with_only_columns(
            func.gen_random_uuid(),
            Item.id,
            # Explicit casts, not bare parameters. In an INSERT ... SELECT the
            # driver prepares the SELECT before the target columns are in
            # scope, so an uncast parameter can fail type inference outright
            # rather than quietly defaulting to something workable.
            cast(literal(event_type.value), ItemEvent.__table__.c.event_type.type),
            cast(null(), ItemEvent.__table__.c.actor_id.type),
            cast(literal(payload, type_=JSONB()), JSONB()),
            literal(payload_hash),
            literal(moment),
        ),
    )


async def flag_actor(
    session: AsyncSession,
    actor_id: uuid.UUID,
    reason: str,
    flagged_by: uuid.UUID | None,
) -> FlagOutcome:
    """Flag an actor and dispute everything they registered. Caller commits.

    One transaction: the flag, the audit event, the dispute rows, the item
    status and the provenance events all land together or not at all. A flag
    that applied to the user row but not to their items would leave the system
    displaying records it has already decided not to stand behind.
    """
    actor = await _load_actor(session, actor_id)

    if actor.fraud_flagged_at is not None:
        logger.info("reputation.flag.already_flagged", actor_id=str(actor_id))
        return FlagOutcome(
            actor_id=actor_id,
            items_affected=0,
            attestations_affected=0,
            already_in_state=True,
        )

    moment = now()
    actor.fraud_flagged_at = moment

    registered = select(Item.id).where(Item.registered_by == actor_id)

    # 1. One dispute row per registered item, stamped with the actor whose flag
    #    caused it so the reversal can find exactly these rows again.
    dispute_columns = ItemDispute.__table__.c
    inserted = await session.execute(
        pg_insert(ItemDispute)
        .from_select(
            ["id", "item_id", "source", "reason", "triggered_by", "raised_by", "raised_at"],
            registered.with_only_columns(
                func.gen_random_uuid(),
                Item.id,
                cast(literal(DisputeSource.FRAUD_FLAG.value), dispute_columns.source.type),
                literal(reason),
                cast(literal(actor_id), dispute_columns.triggered_by.type),
                cast(literal(flagged_by), dispute_columns.raised_by.type),
                literal(moment),
            ),
        )
        # A re-flag after a clear inserts fresh rows; a flag applied twice
        # concurrently collides on the partial unique index and does nothing.
        # The index is partial, so its predicate has to be named too or
        # Postgres cannot tell which index is being inferred.
        .on_conflict_do_nothing(
            index_elements=["item_id", "source"],
            index_where=ItemDispute.cleared_at.is_(None),
        )
    )

    # 2. The summary column, so Phase 6 reads and serialisers need no changes.
    await session.execute(
        update(Item)
        .where(Item.registered_by == actor_id, Item.dispute_status != DisputeStatus.DISPUTED)
        .values(dispute_status=DisputeStatus.DISPUTED)
    )

    # 3. One provenance event per item, so the item's own history explains why
    #    it changed rather than the status shifting under the reader.
    await session.execute(
        _bulk_item_events(
            registered,
            ItemEventType.DISPUTED,
            {
                "source": DisputeSource.FRAUD_FLAG.value,
                "reason": reason,
                "triggered_by_actor": str(actor_id),
            },
        )
    )

    attested_count = await session.scalar(
        select(func.count()).select_from(
            select(Attestation.id).where(Attestation.attestor_id == actor_id).subquery()
        )
    )

    session.add(
        AuthEvent(
            user_id=actor_id,
            event_type=AuthEventType.FRAUD_FLAG,
            detail={
                "reason": reason,
                "flagged_by": str(flagged_by) if flagged_by else None,
                "items_disputed": _rows(inserted),
                "attestations_disqualified": attested_count or 0,
            },
        )
    )

    outcome = FlagOutcome(
        actor_id=actor_id,
        items_affected=_rows(inserted),
        attestations_affected=int(attested_count or 0),
        already_in_state=False,
    )
    logger.warning(
        "reputation.actor_flagged",
        **outcome.as_log_fields(),
        reason=reason,
        effect="registered items are DISPUTED; attested items lose this actor's "
        "contribution on their next read, with no cache to invalidate",
    )
    return outcome


async def clear_fraud_flag(
    session: AsyncSession,
    actor_id: uuid.UUID,
    cleared_by: uuid.UUID | None,
    reason: str = "",
) -> FlagOutcome:
    """Lift a flag and the disputes it caused, and nothing else. Caller commits."""
    actor = await _load_actor(session, actor_id)

    if actor.fraud_flagged_at is None:
        logger.info("reputation.clear.not_flagged", actor_id=str(actor_id))
        return FlagOutcome(
            actor_id=actor_id,
            items_affected=0,
            attestations_affected=0,
            already_in_state=True,
        )

    moment = now()
    actor.fraud_flagged_at = None

    # 1. Close exactly the disputes this flag opened. `triggered_by` is what
    #    makes this selective; matching on source alone would also close a
    #    dispute raised by a different actor's flag against the same item.
    closed = await session.execute(
        update(ItemDispute)
        .where(
            ItemDispute.triggered_by == actor_id,
            ItemDispute.source == DisputeSource.FRAUD_FLAG,
            ItemDispute.cleared_at.is_(None),
        )
        .values(cleared_at=moment, cleared_by=cleared_by)
        .returning(ItemDispute.item_id)
    )
    touched = [row[0] for row in closed.all()]

    if not touched:
        session.add(
            AuthEvent(
                user_id=actor_id,
                event_type=AuthEventType.FRAUD_CLEAR,
                detail={"reason": reason, "cleared_by": str(cleared_by) if cleared_by else None,
                        "items_restored": 0},
            )
        )
        return FlagOutcome(
            actor_id=actor_id, items_affected=0, attestations_affected=0, already_in_state=False
        )

    # 2. Restore only items with no *other* open dispute. An inspector's
    #    independent finding is not this admin's to lift, and the NOT EXISTS is
    #    evaluated in the database so two concurrent clears cannot each conclude
    #    the item is now clear.
    still_open = exists().where(
        and_(ItemDispute.item_id == Item.id, ItemDispute.cleared_at.is_(None))
    )
    restorable = select(Item.id).where(Item.id.in_(touched), not_(still_open))

    await session.execute(
        _bulk_item_events(
            restorable,
            ItemEventType.DISPUTE_CLEARED,
            {
                "source": DisputeSource.FRAUD_FLAG.value,
                "reason": reason or "fraud flag lifted",
                "triggered_by_actor": str(actor_id),
            },
        )
    )

    restored = await session.execute(
        update(Item)
        .where(Item.id.in_(touched), not_(still_open))
        .values(dispute_status=DisputeStatus.NONE)
    )

    session.add(
        AuthEvent(
            user_id=actor_id,
            event_type=AuthEventType.FRAUD_CLEAR,
            detail={
                "reason": reason,
                "cleared_by": str(cleared_by) if cleared_by else None,
                "disputes_closed": len(touched),
                "items_restored": _rows(restored),
            },
        )
    )

    outcome = FlagOutcome(
        actor_id=actor_id,
        items_affected=_rows(restored),
        attestations_affected=len(touched),
        already_in_state=False,
    )
    logger.warning(
        "reputation.flag_cleared",
        **outcome.as_log_fields(),
        disputes_closed=len(touched),
        note="items carrying an independent dispute stay disputed",
    )
    return outcome


async def raise_dispute(
    session: AsyncSession,
    item_id: uuid.UUID,
    source: DisputeSource,
    reason: str,
    raised_by: uuid.UUID | None,
) -> ItemDispute | None:
    """Open one dispute against one item. Caller commits.

    Returns ``None`` when an open dispute of that source already exists, so a
    repeated call is a no-op rather than a second reason saying the same thing.
    """
    existing = (
        await session.execute(
            select(ItemDispute).where(
                ItemDispute.item_id == item_id,
                ItemDispute.source == source,
                ItemDispute.cleared_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    dispute = ItemDispute(
        item_id=item_id,
        source=source,
        reason=reason,
        raised_by=raised_by,
    )
    session.add(dispute)

    item = await session.get(Item, item_id)
    if item is not None:
        item.dispute_status = DisputeStatus.DISPUTED

    session.add(
        ItemEvent(
            item_id=item_id,
            event_type=ItemEventType.DISPUTED,
            actor_id=raised_by,
            payload={"source": source.value, "reason": reason},
            payload_hash=hash_object({"source": source.value, "reason": reason}),
        )
    )
    await session.flush()
    return dispute


async def open_dispute_for(session: AsyncSession, item_id: uuid.UUID) -> ItemDispute | None:
    """The oldest open dispute against an item, or ``None``."""
    return (
        await session.execute(
            select(ItemDispute)
            .where(ItemDispute.item_id == item_id, ItemDispute.cleared_at.is_(None))
            .order_by(ItemDispute.raised_at)
            .limit(1)
        )
    ).scalar_one_or_none()
